import pickle
import re
import time
import urllib.parse
import unicodedata

from bs4 import BeautifulSoup
from resources.lib.ui import control, database
from resources.lib.ui.BrowserBase import BrowserBase

class Sources(BrowserBase):
    _BASE_URL = 'https://animeav1.com'

    # Standard headers to simulate a browser and avoid basic blocks
    _HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': f'{_BASE_URL}/'
    }

    # Maximum search results to check from catalog. The catalog returns 20 articles per page,
    # so this value should not exceed 20 unless pagination is implemented.
    _MAX_SEARCH_RESULTS = 20

    def __init__(self):
        self.run_id = int(time.time())
        self.current_mal_id = None

    def _dbg(self, message, level='info'):
        control.log(f"[AnimeAV1 RUN {self.run_id}] {message}", level=level)

    def get_sources(self, mal_id, episode):
        self._dbg(f" Started. MAL ID={mal_id}, Episode={episode}")

        self.current_mal_id = mal_id

        show = database.get_show(mal_id)
        if not show:
            self._dbg(f"✗ No data on database for MAL ID={mal_id}, Episode={episode}")
            return []

        try:
            kodi_meta = pickle.loads(show.get('kodi_meta'))
        except Exception:
            kodi_meta = {}

        title_oficial = self._clean_title(kodi_meta.get('name') or '')
        #title_oficial = kodi_meta.get('name') or ''
        if not title_oficial:
            self._dbg("✗ kodi_meta has no usable name. Early exit.")
            return []

        try:
            ep_num = int(episode)
        except (TypeError, ValueError):
            self._dbg(f"✗ Invalid episode received: {episode}", level='WARNING')
            return []

        serie_link = self._find_series_link(title_oficial)
        if not serie_link:
            self._dbg("No exact series match found in catalog.")
            return []
        self._dbg(f"Series found: {serie_link}")

        episodio_link = serie_link + '/' + str(ep_num)
        
        html = self._get_request(episodio_link, headers=self._HEADERS)
        if not html:
            self._dbg("✗ Episode page returned no HTML (empty request or error).", level='WARNING')
            return []
        
        self._dbg(f"Episode found: {episodio_link}")

        # Pass mal_id to extraction for Otaku Skip Intro
        sources = self._extract_video(episodio_link, title_oficial, ep_num, self.current_mal_id)
        self._dbg(f"RUN {self.run_id} finished with {len(sources)} source(s)")
        return sources

    def _extract_mal_id_from_page(self, url):
        html = self._get_request(url, headers=self._HEADERS)
        if not html:
            self._dbg("✗ Anime page returned no HTML (empty request or error).", level='WARNING')
            return None

        anime_mal_match = re.search(r'malId\s*:\s*(\d+),', html)
        if anime_mal_match:
            mal_id = int(anime_mal_match.group(1))
            return mal_id

        # Fallback 1: Try JSON-style with quotes around key
        json_pattern = r'"malId"\s*:\s*(\d+)'
        match = re.search(json_pattern, html)
        if match:
            return int(match.group(1))

        # Fallback 2: First occurrence as absolute last resort
        fallback_pattern = r',malId\s*:\s*(\d+)'
        match = re.search(fallback_pattern, html)
        if match:
            return int(match.group(1))

        self._dbg(f"✗ No MAL ID found in url: {url}", level='WARNING')
        return None

    def _find_series_link(self, title_oficial):
        # Replace spaces with '+' for URL query (as per user request)
        search_title = title_oficial.replace(' ', '+')
        search_url = f"{self._BASE_URL}/catalogo?search={urllib.parse.quote(search_title)}"

        html_search = self._get_request(search_url, headers=self._HEADERS)
        if not html_search:
            self._dbg("✗ Search page returned no HTML (empty request or error).", level='WARNING')
            return None

        soup_search = BeautifulSoup(html_search, "html.parser")
        articulos = soup_search.find_all('article', class_=lambda c: c and 'group/item' in c and 'text-body' in c)

        candidates = articulos[:self._MAX_SEARCH_RESULTS]

        if not candidates:
            self._dbg("✗ No candidates found in catalog results.", level='WARNING')
            return None

        expected_id = None
        try:
            expected_id = int(self.current_mal_id)
        except (TypeError, ValueError):
            self._dbg(f"✗ Invalid current_mal_id type for comparison: {self.current_mal_id}", level='WARNING')

        for i, articulo in enumerate(candidates, 1):
            etiqueta_titulo = articulo.find(['h3', 'h2'])
            if not etiqueta_titulo:
                continue

            raw_title = etiqueta_titulo.get_text(strip=True)
            enlace = articulo.find('a', href=True)
            if not enlace:
                continue

            candidate_link = urllib.parse.urljoin(self._BASE_URL, enlace['href'])

            candidate_mal_id = self._extract_mal_id_from_page(candidate_link)

            if candidate_mal_id is None:
                continue

            if expected_id is not None and candidate_mal_id == expected_id:
                self._dbg(f"✓ MAL ID match found! Anime: '{raw_title}' (MAL ID: {candidate_mal_id})")
                return candidate_link
            
        self._dbg(f"✗ No MAL ID match found after checking {len(candidates)} candidate(s).", level='WARNING')
        return None

    @staticmethod
    def _media_slug_from_url(url):
        m = re.search(r'/media/([^/?#]+)', url or '')
        return m.group(1) if m else None

    @staticmethod
    def _looks_like_episode_page(html_text):
        if not html_text:
            return False
        # Robust markers observed on the episode page:
        # - blocks like "embeds: { SUB: [...], DUB: [...] }"
        return (
                ('embeds:' in html_text and 'downloads:' in html_text)
                or ('Episodio' in html_text and 'player.zilla-networks.com' in html_text)
        )

    def _extract_video(self, url, title, episode, mal_id=None):
        html_episodio = self._get_request(url, headers=self._HEADERS)
        if not html_episodio:
            self._dbg("✗ Video page returned no HTML (empty request or error).", level='WARNING')
            return []

        embeds = self._parse_embeds(html_episodio)
        sub_servers = embeds.get('SUB', [])

        # Block problematic or unsupported servers
        # PixelDrain, TeraBox, HLS, UPNShare (Mega is reliable and allowed)
        blocked_servers = {'HLS', 'UPNSHARE', 'PIXELDRAIN', 'TERABOX'}

        reliable_sources = []
        hls_sources = []

        for item in sub_servers:
            server_name = item.get('server', '')
            raw_url = item.get('url', '')

            clean_server = server_name.upper()

            # Exclude blocked servers
            if any(block in clean_server for block in blocked_servers):
                continue

            if not raw_url:
                continue

            video_url = raw_url
            headers = {}

            # MP4Upload specific headers to ensure playback
            if 'MP4UPLOAD' in clean_server or 'YOURUP' in clean_server:
                headers['Referer'] = f'{self._BASE_URL}/'
                headers['Origin'] = f'{self._BASE_URL}'

            label = f'{title} - Ep {episode}'
            if server_name:
                label += f' [{server_name}]'

            # Assign FHD quality (3) to stable servers
            quality = self._get_quality_from_server(server_name)

            # Generate a unique ID for this embed (uses timestamp + index)
            embed_id = f"animeav1_{self.run_id}_{len(reliable_sources)}"

            source = {
                'release_title': label,
                'hash': video_url,
                'type': 'embed',
                'quality': quality,
                'debrid_provider': '',
                'provider': 'animeav1',
                'size': 'NA',
                'seeders': 0,
                'byte_size': 0,
                'info': ['SUB'],
                'lang': 2,
                'channel': 3,
                'sub': 1,
                'mal_id': mal_id if mal_id else '',
                'embed': embed_id,
            }

            if headers:
                source['headers'] = urllib.parse.urlencode(headers)

            source['skip'] = {}

            if 'HLS' in clean_server:
                hls_sources.append(source)
            else:
                reliable_sources.append(source)

        sources = reliable_sources + hls_sources

        # Filter sources without a valid release title (extra safety)
        valid_sources = [s for s in sources if s.get('release_title')]

        return valid_sources

    def _get_quality_from_server(self, server_name):
        if not server_name:
            return 1  # Default to SD if no server name is provided

        name = str(server_name).lower()

        if any(keyword in name for keyword in ['mp4upload', 'streamtape', 'yourupload', 'mega']):
            return 3  # FHD/High

        return 2  # Default to HD for other servers

    def _parse_embeds(self, html_text):
        match = re.search(r'embeds:\s*\{(.*?)\}\s*,\s*downloads:', html_text, re.DOTALL)
        if not match:
            self._dbg("✗ No matches for embeded.", level='WARNING')
            return {}

        embeds_block = match.group(1)

        result = {}
        for lang_match in re.finditer(r'(SUB|DUB):\s*\[(.*?)\]', embeds_block, re.DOTALL):
            lang = lang_match.group(1)
            items_block = lang_match.group(2)
            servers = []
            for item_match in re.finditer(
                    r'server:\s*"((?:[^"\\]|\\.)*)"\s*,\s*url:\s*"((?:[^"\\]|\\.)*)"',
                    items_block
            ):
                servers.append({'server': item_match.group(1), 'url': item_match.group(2)})
            result[lang] = servers

        return result

    def _clean_title(self, title):
        """
        Normalizes the title by stripping accents and special characters.
        Crucial because AnimeAV1 uses accented titles (e.g., Caraméliser).
        """
        if not title:
            return ""
        # 1. Decompose Unicode characters (e.g., é -> e + ´)
        nfkd_form = unicodedata.normalize('NFKD', title)
        # 2. Filter out non-spacing accents/characters
        ascii_title = ''.join([c for c in nfkd_form if not unicodedata.combining(c)])

        # 3. Clean special characters and normalized spaces, then lowercase
        clean_ascii = re.sub(r'[^a-z0-9\s]', '', ascii_title.lower())
        return ' '.join(clean_ascii.split())

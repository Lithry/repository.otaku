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

    def __init__(self):
        self.run_id = int(time.time())
        self.current_mal_id = None

    def _dbg(self, message, level='info'):
        control.log(f"[AnimeAV1 RUN {self.run_id}] {message}", level=level)

    def _log_checklist(self, mal_id, episode):
        """
        Runtime checklist to validate end-to-end behavior in kodi.log.
        If this block never appears during a scrape attempt, provider output likely came from cache.
        """
        self._dbg("=== CHECKLIST START ===")
        self._dbg("1) Verify real execution of the scraper (if this block does not appear, it was cached).")
        self._dbg("2) Validate local metadata: database.get_show + kodi_meta.")
        self._dbg("3) Confirm title_oficial and aliases (Romaji/alternatives).")
        self._dbg("4) Confirm search on AnimeAV1 by name, not by AniList/MAL IDs.")
        self._dbg("5) Confirm exact match of the show (avoiding confusing seasons/prequels).")
        self._dbg("6) Confirm localization of episode via data-mal-sync-episode.")
        self._dbg("7) Confirm parsing of SUB embeds and result counts.")
        self._dbg(f"Current Input -> mal_id={mal_id}, episode={episode}")
        self._dbg("=== CHECKLIST END ===")

    def get_sources(self, mal_id, episode):
        self._dbg(f"RUN {self.run_id} started")
        self._log_checklist(mal_id, episode)
        control.log(f"AnimeAV1: Getting sources for MAL ID {mal_id}, Episode {episode}")

        # Store mal_id globally for _extract_video to use
        self.current_mal_id = mal_id

        show = database.get_show(mal_id)
        if not show:
            self._dbg("database.get_show returned empty; no metadata to resolve title.", level='warning')
            return []

        # Otaku stores encrypted metadata using pickle
        try:
            kodi_meta = pickle.loads(show.get('kodi_meta'))
        except Exception:
            kodi_meta = {}
            self._dbg("Could not deserialize kodi_meta (pickle).", level='warning')

        title_oficial = self._clean_title(kodi_meta.get('name') or '')
        if not title_oficial:
            control.log("AnimeAV1: kodi_meta has no 'name', cannot perform search.", level='info')
            self._dbg("kodi_meta has no usable name; early exit.")
            return []

        aliases = self._get_aliases(kodi_meta)
        self._dbg(f"Official Title='{title_oficial}'")
        self._dbg(f"Normalized Aliases={aliases}")

        try:
            ep_num = int(episode)
        except (TypeError, ValueError):
            control.log(f"AnimeAV1: Invalid episode number: {episode}")
            self._dbg(f"Invalid episode received: {episode}", level='warning')
            return []

        self._dbg("Starting series search by name (not by remote IDs).")
        serie_link = self._find_series_link(title_oficial, aliases)
        if not serie_link:
            control.log(f"AnimeAV1: No exact match found in search for {title_oficial}", level='info')
            self._dbg("No exact series match found in catalog.")
            return []
        self._dbg(f"Series found: {serie_link}")

        episodio_link = self._find_episode_link(serie_link, ep_num)
        if not episodio_link:
            self._dbg("Could not find episode link using data-mal-sync-episode.")
            return []
        self._dbg(f"Episode found: {episodio_link}")

        # Pass mal_id to extraction for Otaku Skip Intro
        sources = self._extract_video(episodio_link, title_oficial, ep_num, mal_id)
        self._dbg(f"RUN {self.run_id} finished with {len(sources)} source(s)")
        return sources

    def _find_series_link(self, title_oficial, aliases):
        """
        STEP 1: SEARCH WITH TOLERANCE.
        Builds multiple candidate search queries and applies robust matches.
        """
        # Build search queries list to try, from most specific to general
        queries = []
        
        # 1. Full official title
        if title_oficial:
            queries.append(title_oficial)
            
        # 2. Split by common separators (:, ., -, ,) and use first part
        for sep in (':', '.', '-', ','):
            if sep in title_oficial:
                part = title_oficial.split(sep)[0].strip()
                if len(part.split()) >= 2:  # Avoid too generic single-word queries
                    queries.append(part)
                    
        # 3. Shorten extremely long titles (use first 4 words)
        words = title_oficial.split()
        if len(words) > 4:
            queries.append(' '.join(words[:4]))
            
        # 4. Try aliases
        for alias in aliases:
            if alias and len(alias.split()) >= 2:
                queries.append(alias)
                
        # Deduplicate while preserving insertion order
        unique_queries = []
        for q in queries:
            if q not in unique_queries:
                unique_queries.append(q)
                
        self._dbg(f"Generated search queries list to try sequentially: {unique_queries}")
        
        # Prepare helper variables for match checks
        title_nospace = title_oficial.replace(' ', '')
        alias_nospace_list = [a.replace(' ', '') for a in aliases if a]
        title_words = set(title_oficial.split())

        for query in unique_queries:
            search_url = f"{self._BASE_URL}/catalogo?search={urllib.parse.quote(query)}"
            self._dbg(f"Searching catalog with query: '{query}' -> {search_url}")

            html_search = self._get_request(search_url, headers=self._HEADERS)
            if not html_search:
                self._dbg(f"Query '{query}' returned no HTML, skipping.", level='warning')
                continue

            soup_search = BeautifulSoup(html_search, "html.parser")
            articulos = soup_search.find_all('article', class_=lambda c: c and 'group/item' in c and 'text-body' in c)
            self._dbg(f"Found {len(articulos)} candidate(s) for query '{query}'")

            for articulo in articulos:
                etiqueta_titulo = articulo.find(['h3', 'h2'])
                if not etiqueta_titulo:
                    continue

                raw_found_title = etiqueta_titulo.get_text(strip=True)
                t_clean = self._clean_title(raw_found_title)
                t_nospace = t_clean.replace(' ', '')

                # A. Exact Match (standard)
                if t_clean == title_oficial or t_clean in aliases:
                    enlace = articulo.find('a', href=True)
                    if enlace:
                        self._dbg(f"Match found! Exact title match: '{raw_found_title}'")
                        return urllib.parse.urljoin(self._BASE_URL, enlace['href'])

                # B. Space-insensitive Exact Match (e.g. 'tataki tsubushimasu' vs 'tatakitsubushimasu')
                if t_nospace == title_nospace or t_nospace in alias_nospace_list:
                    enlace = articulo.find('a', href=True)
                    if enlace:
                        self._dbg(f"Match found! Space-insensitive match: '{raw_found_title}'")
                        return urllib.parse.urljoin(self._BASE_URL, enlace['href'])

                # C. Space-insensitive Substring Match (one is inside the other)
                if t_nospace and title_nospace and (t_nospace in title_nospace or title_nospace in t_nospace):
                    enlace = articulo.find('a', href=True)
                    if enlace:
                        self._dbg(f"Match found! Space-insensitive substring match: '{raw_found_title}'")
                        return urllib.parse.urljoin(self._BASE_URL, enlace['href'])

                # D. Word Token Overlap Match (at least 70% of words in title_oficial must match)
                t_words = set(t_clean.split())
                if title_words and t_words:
                    intersection = title_words & t_words
                    overlap_ratio = len(intersection) / len(title_words)
                    if overlap_ratio >= 0.70:
                        enlace = articulo.find('a', href=True)
                        if enlace:
                            self._dbg(f"Match found! Word overlap match ({int(overlap_ratio*100)}%): '{raw_found_title}'")
                            return urllib.parse.urljoin(self._BASE_URL, enlace['href'])

        self._dbg("No match found after trying all search queries and matching strategies.", level='warning')
        return None

    def _find_episode_link(self, serie_link, ep_num):
        """
        STEP 2: SERIES PAGE AND EPISODE SELECTION.
        AnimeAV1 marks each episode card with the attribute data-mal-sync-episode,
        which matches 1:1 with MAL's episode number.
        """
        html_anime = self._get_request(serie_link, headers=self._HEADERS)

        if not html_anime:
            self._dbg("Series page returned no HTML (empty request or error).", level='warning')
            return None

        soup_anime = BeautifulSoup(html_anime, "html.parser")
        ep_article = soup_anime.find('article', attrs={'data-mal-sync-episode': str(ep_num)})

        if ep_article:
            ep_link_tag = ep_article.find('a', href=True)
            if ep_link_tag:
                self._dbg(f"Match found via data-mal-sync-episode: {ep_link_tag['href']}")
                return urllib.parse.urljoin(self._BASE_URL, ep_link_tag['href'])

        # Fallback 0: Search canonical /media/<slug>/<ep_num> links across the entire page.
        media_slug = self._media_slug_from_url(serie_link)
        if media_slug:
            self._dbg(f"Attempting fallback 0: canonical slug '/media/{media_slug}/{ep_num}'")
            for a in soup_anime.find_all('a', href=True):
                href = a.get('href') or ''
                if re.search(rf'/media/{re.escape(media_slug)}/{ep_num}(?:[/?#]|$)', href):
                    resolved = urllib.parse.urljoin(self._BASE_URL, href)
                    self._dbg(f"Fallback 0 canonical href successful for ep {ep_num}: {resolved}")
                    return resolved

        # Fallback 1: Some titles don't expose data-mal-sync-episode but display links like "Watch <title> <ep>".
        self._dbg(f"Episode {ep_num} not present via data-mal-sync-episode. Testing fallback 1 (anchors).")
        for a in soup_anime.find_all('a', href=True):
            txt = ' '.join((a.get_text(' ', strip=True) or '').split())
            low = txt.lower()
            if not txt or 'movie' in low or 'ova' in low:
                continue

            parent_txt = ' '.join((a.parent.get_text(' ', strip=True) if a.parent else '').split())
            title_attr = (a.get('title') or '').strip()
            haystack = ' | '.join([txt, parent_txt, title_attr])

            # Attempt to extract episode number robustly.
            candidates = []
            m = re.search(r'(?i)\b(?:episodio|episode|ep)\s*(\d{1,4})\b', haystack)
            if m:
                candidates.append(int(m.group(1)))
            m = re.search(r'(\d{1,4})\s*$', txt)
            if m:
                candidates.append(int(m.group(1)))
            href = a.get('href') or ''
            m = re.search(r'/(\d{1,4})(?:[/?#]|$)', href)
            if m:
                candidates.append(int(m.group(1)))

            if ep_num in candidates:
                resolved = urllib.parse.urljoin(self._BASE_URL, href)
                self._dbg(f"Fallback 1 via anchor successful for ep {ep_num}: {resolved}")
                return resolved

        # Fallback 2: Generic cards with data-episode / data-ep attributes.
        self._dbg("Testing fallback 2 (data-episode attributes).")
        for tag in soup_anime.find_all(attrs={'data-episode': True}):
            try:
                if int(tag.get('data-episode')) == ep_num:
                    lk = tag.find('a', href=True)
                    if lk:
                        resolved = urllib.parse.urljoin(self._BASE_URL, lk['href'])
                        self._dbg(f"Fallback 2 via data-episode successful for ep {ep_num}: {resolved}")
                        return resolved
            except Exception as e:
                self._dbg(f"Error checking data-episode: {str(e)}", level='warning')

        for tag in soup_anime.find_all(attrs={'data-ep': True}):
            try:
                if int(tag.get('data-ep')) == ep_num:
                    lk = tag.find('a', href=True)
                    if lk:
                        resolved = urllib.parse.urljoin(self._BASE_URL, lk['href'])
                        self._dbg(f"Fallback 2 via data-ep successful for ep {ep_num}: {resolved}")
                        return resolved
            except Exception as e:
                self._dbg(f"Error checking data-ep: {str(e)}", level='warning')

        # Final fallback: Build direct URL /media/<slug>/<ep> and validate.
        if media_slug:
            direct_url = f"{self._BASE_URL}/media/{media_slug}/{ep_num}"
            self._dbg(f"Testing fallback 3 (direct URL): {direct_url}")
            direct_html = self._get_request(direct_url, headers=self._HEADERS)
            if direct_html and self._looks_like_episode_page(direct_html):
                self._dbg(f"Fallback 3 direct URL successful for ep {ep_num}: {direct_url}")
                return direct_url

        control.log(f"AnimeAV1: Episode {ep_num} not found on series page.")
        self._dbg(f"No match for episode {ep_num} after trying all fallbacks.", level='info')
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
        """
        STEP 3: FINAL EXTRACTION.
        Blocks problematic servers and assigns FHD quality to stable ones.
        Injects mal_id for Otaku Skip Intro.
        """
        html_episodio = self._get_request(url, headers=self._HEADERS)
        if not html_episodio:
            self._dbg("Episode page returned no HTML.", level='warning')
            return []

        embeds = self._parse_embeds(html_episodio)
        sub_servers = embeds.get('SUB', [])
        self._dbg(f"Parsed {len(sub_servers)} SUB server(s) from episode page.")

        # Block problematic or unsupported servers
        # PixelDrain, TeraBox, HLS, UPNShare, Mega
        blocked_servers = {'HLS', 'UPNSHARE', 'MEGA', 'PIXELDRAIN', 'TERABOX'}

        reliable_sources = []
        hls_sources = []

        for item in sub_servers:
            server_name = item.get('server', '')
            raw_url = item.get('url', '')

            clean_server = server_name.upper()

            # Exclude blocked servers
            if any(block in clean_server for block in blocked_servers):
                self._dbg(f"Excluding server: {server_name}")
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
                # Add mal_id for Otaku Skip Intro
                'mal_id': mal_id if mal_id else '',
                # Unique embed ID so the player can save/retrieve skip intro/outro
                'embed': embed_id,
            }

            if headers:
                source['headers'] = urllib.parse.urlencode(headers)

            source['skip'] = {}  # Skip intro/outro storage map

            # Separate HLS to put it at the end (though blocked above if Zilla)
            if 'HLS' in clean_server:
                hls_sources.append(source)
            else:
                reliable_sources.append(source)

        sources = reliable_sources + hls_sources

        # Filter sources without a valid release title (extra safety)
        valid_sources = [s for s in sources if s.get('release_title')]

        self._dbg(f"Returning {len(valid_sources)} filtered source(s).")
        return valid_sources

    def _get_quality_from_server(self, server_name):
        """
        Assigns FHD quality (3) to MP4Upload, StreamTape, and YouTubUp.
        Uses robust case-insensitive logic.
        """
        if not server_name:
            return 1  # Default to SD if no server name is provided

        name = str(server_name).lower()

        if any(keyword in name for keyword in ['mp4upload', 'streamtape', 'yourupload']):
            return 3  # FHD/High

        return 2  # Default to HD for other servers

    def _parse_embeds(self, html_text):
        """
        Extracts {'SUB': [{'server':.., 'url':..}, ...], 'DUB': [...]} from the
        `embeds: { SUB: [...], DUB: [...] }` block SvelteKit injects in a
        <script> tag on the episode page. We avoid json.loads() because the
        block is not strict JSON (unquoted keys, values like `void 0`, etc.);
        instead, we extract the server/url via regex, which is highly robust.
        """
        match = re.search(r'embeds:\s*\{(.*?)\}\s*,\s*downloads:', html_text, re.DOTALL)
        if not match:
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

    def _get_aliases(self, kodi_meta):
        """
        Helper function to fetch alternative titles of the anime (Romaji, English).
        Ensures search doesn't fail if AnimeAV1 uses a slightly different title format.
        """
        aliases = []
        if not kodi_meta:
            return aliases

        # Use list(...) to avoid mutating the cached dict in kodi_meta,
        # and fallback empty list to prevent crashes if key is None.
        raw_aliases = list(kodi_meta.get('aliases') or [])
        if kodi_meta.get('ename'):
            raw_aliases.append(kodi_meta.get('ename'))
        if kodi_meta.get('jname'):
            raw_aliases.append(kodi_meta.get('jname'))

        for a in raw_aliases:
            if a:
                aliases.append(self._clean_title(a))
        return aliases

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

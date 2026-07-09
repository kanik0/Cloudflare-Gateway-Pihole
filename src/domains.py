import os
import http.client
from urllib.parse import urlparse, urljoin
from configparser import ConfigParser
from src import info, silent_error, error
from src.convert import convert_to_block_list, convert_to_allow_list
from src.requests import retry, retry_config, RateLimitException, HTTPException


class BaseDomainConverter:
    def read_urls_from_file(self, filename):
        urls = []
        try:
            config = ConfigParser()
            config.read(filename)
            for section in config.sections():
                for key in config.options(section):
                    if not key.startswith("#"):
                        urls.append(config.get(section, key))
        except Exception:
            with open(filename, "r") as file:
                urls = [
                    url.strip() for url in file if not url.startswith("#") and url.strip()
                ]
        return urls

    def read_urls_from_env(self, env_var):
        urls = os.getenv(env_var, "")
        return [url.strip() for url in urls.split() if url.strip()]

    def read_urls(self, env_var, file_path):
        urls = self.read_urls_from_file(file_path)
        urls += self.read_urls_from_env(env_var)
        return urls

    @retry(**retry_config)
    def download_file(self, url, timeout: int = 15, max_redirects: int = 5):
        headers = {"User-Agent": "Mozilla/5.0"}
        redirects = 0

        while True:
            parsed_url = urlparse(url)
            path = parsed_url.path or "/"
            if parsed_url.query:
                path += f"?{parsed_url.query}"

            if parsed_url.scheme == "https":
                conn = http.client.HTTPSConnection(parsed_url.netloc, timeout=timeout)
            else:
                conn = http.client.HTTPConnection(parsed_url.netloc, timeout=timeout)

            try:
                conn.request("GET", path, headers=headers)
                response = conn.getresponse()

                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    response.read()  # drain body before closing/reusing
                    if not location:
                        break
                    redirects += 1
                    if redirects > max_redirects:
                        error_message = f"Too many redirects ({redirects}) while downloading {url}"
                        silent_error(error_message)
                        raise HTTPException(error_message)
                    if not urlparse(location).netloc:
                        location = urljoin(url, location)
                    url = location
                    continue

                if response.status != 200:
                    error_message = f"Failed to download file from {url}, status code: {response.status}"
                    silent_error(error_message)
                    if response.status == 429:
                        raise RateLimitException(error_message)
                    else:
                        raise HTTPException(error_message)

                data = response.read().decode("utf-8")
                info(f"Downloaded file from {url}. File size: {len(data)}")
                return data
            finally:
                conn.close()



class BlockDomainConverter(BaseDomainConverter):
    """Processes adlists for block rules. Whitelist subtraction is NOT done here
    because the Allow rule in Cloudflare Gateway takes precedence."""

    def __init__(self):
        self.adlist_urls = self.read_urls("ADLIST_URLS", "./lists/adlist.ini")

    def process_urls(self):
        block_content = ""
        for url in self.adlist_urls:
            block_content += self.download_file(url)

        dynamic_blacklist = os.getenv("DYNAMIC_BLACKLIST", "")
        if dynamic_blacklist:
            block_content += dynamic_blacklist
        else:
            with open("./lists/dynamic_blacklist.txt", "r") as f:
                block_content += f.read()

        return convert_to_block_list(block_content)


class AllowDomainConverter(BaseDomainConverter):
    """Processes whitelists for allow rules."""

    def __init__(self):
        self.whitelist_urls = self.read_urls("WHITELIST_URLS", "./lists/whitelist.ini")

    def process_urls(self):
        white_content = ""
        for url in self.whitelist_urls:
            white_content += self.download_file(url)

        dynamic_whitelist = os.getenv("DYNAMIC_WHITELIST", "")
        if dynamic_whitelist:
            white_content += dynamic_whitelist
        else:
            with open("./lists/dynamic_whitelist.txt", "r") as f:
                white_content += f.read()

        return convert_to_allow_list(white_content)

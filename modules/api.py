# imports: standard
import os
import json
import html
from urllib.parse import quote, unquote
from configparser import ConfigParser
from colorama import Fore, init, Style
import requests
import time

# imports: custom
import modules.cookies

class LoginException(Exception):
    pass

class RequestException(Exception):
    pass


class WhatAPI:
    def __init__(self, interval, api_key=None, cookies=None,
                 server="https://ssl.what.cd"):
        self.session = requests.Session()
        self.session.headers = {
            'Content-type': 'application/x-www-form-urlencoded',
            'Accept-Charset': 'utf-8',
            'User-Agent': 'whatapi [isaaczafuta]',
            **{'Authorization': api_key}
        }
        self.authkey = None
        self.passkey = None
        self.server = server
        self.last_request_time = 0
        self.interval = interval + 0.1  # seconds between requests

        if api_key == None:
            if cookies:
                self.session.cookies = cookies
                try:
                    self._auth()
                except RequestException as e:
                    print(f"{Fore.RED}Failed to authenticate with cookies: {e}{Style.RESET_ALL}")
                    self._login()
            else:
                self._login()
        
        modules.cookies.save_cookies(self.session.cookies)

    def wait(self):
        now = time.monotonic()
        elapsed = now - self.last_request_time
        if elapsed < self.interval:
            print(f"{Fore.CYAN}Sleep for {self.interval - elapsed}s{Style.RESET_ALL}")
            time.sleep(self.interval - elapsed)
        self.last_request_time = time.monotonic()

    def _auth(self):
        '''Gets auth key from server'''
        accountinfo = self.request("index")
        self.authkey = accountinfo["response"]["authkey"]
        self.passkey = accountinfo["response"]["passkey"]

    def _login(self):
        '''Logs in user'''
        loginpage = self.server + '/login.php'
        data = {'username': self.username,
                'password': self.password,
                'keeplogged': 1,
                'login': 'Login'
        }
        r = self.session.post(loginpage, data=data, allow_redirects=False)
        if r.status_code != 302:
            raise LoginException
        self._auth()

    def logout(self):
        '''Logs out user'''
        logoutpage = self.server + '/logout.php'
        params = {'auth': self.authkey}
        self.session.get(logoutpage, params=params, allow_redirects=False)

    def request(self, action, **kwargs):
        '''Makes an AJAX request at a given action page'''
        ajaxpage = self.server + '/ajax.php'
        params = {'action': action}
        if self.authkey:
            params['auth'] = self.authkey
        params.update(kwargs)

        self.wait()  # respect rate limit

        r = self.session.get(ajaxpage, params=params, allow_redirects=False)
        try:
            json_response = r.json()
            if json_response["status"] != "success":
                raise RequestException
            return json_response
        except ValueError:
            raise RequestException

    # get torrent object by id
    def torrent(self, torrent_id):
        torrent_object = {}
        print("Getting torrent by id: %s", torrent_id)
        try:
            torrent_lookup = self.request(
                    'torrent',
                    id=torrent_id
            )
        except Exception as e:
            print(f"{Fore.RED}Failed to get torrent by id {torrent_id}. Error: {e}{Style.RESET_ALL}")
            return torrent_object  # return empty dict on error
        torrent_lookup_status = torrent_lookup.get('status', None)
        if torrent_lookup_status == "success":
            print("Torrent lookup successful")
            torrent_object = torrent_lookup["response"]["torrent"]
            torrent_object['fileList'] = self.parse_file_list(torrent_object.get('fileList', ''))
        else:
            print(f"{Fore.RED}Torrent lookup failed. status = {torrent_lookup_status}{Style.RESET_ALL}")
        # return torrent object
        return torrent_object
    
    def parse_file_list(self, file_list_str):
        """
        Parse the file list from a torrent object.
        The file list is expected to be a string with entries separated by '|||'.
        Each entry is in the format 'filename{{{filesize}}}'.
        """
        file_list = {}
        if file_list_str:
            print("Parsing file list")
            # split the string into individual entries
            entries = file_list_str.split('|||')
            for entry in entries:
                # split filename and filesize
                parts = entry.split('{{{')
                if len(parts) == 2:
                    filename = html.unescape(parts[0].strip())
                    filesize = parts[1].rstrip('}}}').strip()
                    file_list[filename] = int(filesize)
                else:
                    print(f"{Fore.YELLOW}Malformed entry in file list: {entry}{Style.RESET_ALL}")
        else:
            print(f"{Fore.YELLOW}File list is empty or None{Style.RESET_ALL}")

        return file_list


    def search_torrent_by_filename(self, filename):
        params = {
            "filelist": filename
        }
        try:
            response = self.request("browse", **params)
            return response
        except Exception as e:
            print(f"{Fore.RED}Error searching for torrent by filename '{filename}': {e}{Style.RESET_ALL}")
            return {}
    
    def download_torrent(self, torrent_id):
        """
        Download a torrent by its ID.
        Returns the content of the torrent file.
        """
        max_retries = 8
        for attempt in range(max_retries):
            try:
                self.wait()
                ajaxpage = self.server + '/ajax.php'
                response = self.session.get(ajaxpage, params={"action": "download", "id": torrent_id})
                if response.status_code != 200:
                    print(f"{Fore.RED}Status of request is {response.status_code}. Aborting...{Style.RESET_ALL}")
                    print(f"{Fore.RED}Response content: {response._content}{Style.RESET_ALL}")
                
                print("Torrent downloaded successfully")
                return response.content
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"Error downloading torrent: {e}, retrying ({attempt + 1}/{max_retries})...")
                    time.sleep(2)
                else:
                    print(f"{Fore.RED}Failed to download torrent after {max_retries} attempts: {e}{Style.RESET_ALL}")
                    return None
    
    def get_torrent_link(self, torrent_id):
        """
        Get the direct download link for a torrent by its ID.
        """
        return f"{self.server}/torrents.php?action=download&id={torrent_id}&authkey={self.authkey}&torrent_pass={self.passkey}"
    
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
import modules.cookies, modules.logger

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
            **({'Authorization': api_key} if api_key else {})
        }
        self.authkey = None
        self.passkey = None
        self.server = server
        self.last_request_time = 0
        self.interval = interval + 0.1  # seconds between requests
        self.logger = modules.logger.ColorLogger(loglevel=modules.logger.global_loglevel)

        if api_key == None:
            if cookies:
                self.session.cookies = cookies
                try:
                    self._auth()
                except RequestException as e:
                    self.logger.error(f"Failed to authenticate with cookies: {e}")
                finally:
                    modules.cookies.save_cookies(self.session.cookies)

    def wait(self):
        now = time.monotonic()
        elapsed = now - self.last_request_time
        if elapsed < self.interval:
            self.logger.debug(f"Sleep for {self.interval - elapsed:.2f} seconds")
            time.sleep(self.interval - elapsed)
        self.last_request_time = time.monotonic()

    def _auth(self):
        '''Gets auth key from server'''
        accountinfo = self.request("index")
        self.authkey = accountinfo["response"]["authkey"]
        self.passkey = accountinfo["response"]["passkey"]

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
        self.logger.debug(f"Getting torrent by id: {torrent_id}")
        try:
            torrent_lookup = self.request(
                    'torrent',
                    id=torrent_id
            )
        except Exception as e:
            self.logger.error(f"Failed to get torrent by id {torrent_id}. Error: {e}")
            return torrent_object  # return empty dict on error
        torrent_lookup_status = torrent_lookup.get('status', None)
        if torrent_lookup_status == "success":
            self.logger.debug(f"Torrent lookup successful for id: {torrent_id}")
            torrent_object = torrent_lookup["response"]["torrent"]
            torrent_object['fileList'] = self.parse_file_list(torrent_object.get('fileList', ''))
        else:
            self.logger.error(f"Torrent lookup failed for id: {torrent_id}. Status: {torrent_lookup_status}")
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
            self.logger.debug("Parsing file list")
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
                    self.logger.warning(f"Malformed entry in file list: {entry}")
        else:
            self.logger.warning("File list is empty or None")

        return file_list


    def search_torrent_by_filename(self, filename):
        params = {
            "filelist": filename
        }
        try:
            response = self.request("browse", **params)
            return response
        except Exception as e:
            self.logger.error(f"Error searching for torrent by filename '{filename}': {e}")
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
                    self.logger.error(f"Status of request is {response.status_code}. Aborting...")
                    self.logger.error(f"Response content: {response._content}")
                    raise RequestException
                
                self.logger.debug(f"Torrent {torrent_id} downloaded successfully")
                return response.content
            except Exception as e:
                if attempt < max_retries - 1:
                    self.logger.warning(f"Error downloading torrent: {e}, retrying ({attempt + 1}/{max_retries})...")
                    time.sleep(2)
                else:
                    self.logger.error(f"Failed to download torrent after {max_retries} attempts: {e}")
                    return None
    
    def get_torrent_link(self, torrent_id):
        """
        Get the direct download link for a torrent by its ID.
        """
        return f"{self.server}/torrents.php?action=download&id={torrent_id}&authkey={self.authkey}&torrent_pass={self.passkey}"
    
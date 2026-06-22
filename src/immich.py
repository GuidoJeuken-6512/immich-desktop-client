import dbm
import hashlib
import json
import os.path
import shelve
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from time import sleep

import requests


class Immich:
    def __init__(self, immich_host, api_key, album_name=None, album_id=None, device_id=None, shelve_path=None,
                 album_by_year=False, delete_remote_on_local_delete=False):
        self.__immichHost = immich_host
        self.__apiKey = api_key
        self.album_by_year = album_by_year
        self.__delete_remote_on_local_delete = delete_remote_on_local_delete
        self.__album_cache = {}

        if shelve_path is None:
            self.__shelve_path = str(Path.home()) + "/.Immich-desktop-client/shelve"
        else:
            self.__shelve_path = shelve_path

        os.makedirs(os.path.dirname(self.__shelve_path), exist_ok=True)

        if device_id is None:
            self.__uuid = self.__get_uuid()
        else:
            self.__uuid = device_id

        if album_name is None:
            self.album_name = socket.gethostname()
        else:
            self.album_name = album_name

        if self.album_by_year:
            self.__album_id = None
        elif album_id is None:
            self.__album_id = self.__get_or_create_album(self.album_name)
        else:
            self.__album_id = album_id

    def get_matching_files(self, directories, media_file_extensions, recursive=False):
        matching_files = []
        for directory in directories:
            if recursive:
                for root, _, filenames in os.walk(directory):
                    for filename in filenames:
                        if filename.endswith(media_file_extensions):
                            matching_files.append(os.path.join(root, filename))
            else:
                for filename in os.listdir(directory):
                    if filename.endswith(media_file_extensions):
                        matching_files.append(os.path.join(directory, filename))
        return matching_files

    def __sync_with_shelve(self):
        print("catch up with files already stored in shelve")
        try:
            with shelve.open(self.__shelve_path, flag='r') as db:
                data = list(db.keys())
                for key in data:
                    if os.path.isfile(key):
                        if self.__get_sha1(key) != db[key][1]:
                            self.modify(str(key))
                    else:
                        self.delete(key)
                return set(data)
        except dbm.error:
            print("cant open non-existing shelve")
            return set()

    def upload_all_images(self, directories, media_file_extensions, recursive=False, progress_callback=None,
                           should_cancel=None):
        known_files = self.__sync_with_shelve()

        print("scanning directories for files")
        matching_files = self.get_matching_files(directories, media_file_extensions, recursive)
        pending_files = [file for file in matching_files if file not in known_files]

        total = len(pending_files)
        print(f"found {len(matching_files)} file(s), {total} of them new")
        if progress_callback:
            progress_callback(0, total)

        for index, file in enumerate(pending_files, start=1):
            if should_cancel and should_cancel():
                print("upload cancelled")
                break
            try:
                self.created(file, should_cancel=should_cancel)
            except Exception as exc:
                print(f"error when uploading file {file}: {exc}")
            if progress_callback:
                progress_callback(index, total)

    def created(self, file, should_cancel=None):
        try:
            stats = self.__get_file_stats(file)
        except FileNotFoundError:
            print("could not create file")
            return

        headers = {
            'Accept': 'application/json',
            'x-api-key': self.__apiKey,
            'x-Immich-checksum': self.__get_sha1(file)
        }

        data = {
            'deviceAssetId': f"{file}-{stats.st_mtime}",
            'deviceId': self.__uuid,
            'fileCreatedAt': datetime.fromtimestamp(stats.st_mtime),
            'fileModifiedAt': datetime.fromtimestamp(stats.st_mtime),
            'isFavorite': 'false',
        }

        response = self.__request_with_retry("POST", self.__immichHost + "/assets", file, headers, data,
                                              should_cancel)
        if response is None:
            return

        image_id = json.loads(response.text)
        if 'id' not in image_id:
            print(f"error when uploading file {file}: status {response.status_code} {response.text}")
            return

        print("status: " + image_id.get('status', 'unknown'))
        self.__save_image_to_shelve(image_id['id'], file)

        if self.album_by_year:
            year = datetime.fromtimestamp(stats.st_mtime).year
            album_id = self.__get_or_create_album(str(year))
        else:
            album_id = self.__album_id

        self.__add_asset_to_album(image_id['id'], album_id)
        print("saved image successfully: " + str(response.text))

    def __request_with_retry(self, method, url, file, headers, data, should_cancel=None):
        retry_delay = 5
        while not (should_cancel and should_cancel()):
            try:
                with open(file, 'rb') as asset_data:
                    return requests.request(method, url, headers=headers, data=data,
                                             files={'assetData': asset_data})
            except requests.exceptions.RequestException as e:
                print(f"Verbindung verloren bei {method} {file} ({e}), "
                      f"erneuter Versuch in {retry_delay}s ...")
                for _ in range(retry_delay):
                    if should_cancel and should_cancel():
                        break
                    sleep(1)
                retry_delay = min(retry_delay * 2, 60)
        return None

    def modify(self, file, should_cancel=None):
        try:
            asset_id, stored_sha1 = self.__get_image_id_and_sha1(file)
        except (KeyError, *dbm.error):
            print("trying to modify non-uploaded file ... uploading file")
            self.created(file, should_cancel=should_cancel)
            return

        current_sha1 = self.__get_sha1(file)
        if current_sha1 == stored_sha1:
            print(f"no actual content change for {file}, skipping replace")
            return

        try:
            stats = self.__get_file_stats(file)
        except FileNotFoundError:
            print("could not modify file")
            return

        headers = {
            'Accept': 'application/json',
            'x-api-key': self.__apiKey,
            'x-Immich-checksum': current_sha1,
        }
        data = {
            'deviceAssetId': f"{file}-{stats.st_mtime}",
            'deviceId': self.__uuid,
            'fileCreatedAt': datetime.fromtimestamp(stats.st_mtime),
            'fileModifiedAt': datetime.fromtimestamp(stats.st_mtime),
        }

        response = self.__request_with_retry(
            "PUT", f"{self.__immichHost}/assets/{asset_id}/original", file, headers, data, should_cancel)
        if response is None:
            return

        if response.status_code == 200:
            self.__save_image_to_shelve(asset_id, file)
            print("replaced image successfully: " + str(response.text))
        else:
            print(f"error when replacing file: status {response.status_code} {response.text}")

    def delete(self, file):
        try:
            asset_id = self.__get_image_id(file)
        except (KeyError, *dbm.error):
            print("trying to delete non-uploaded file")
            return

        if self.__delete_remote_on_local_delete:
            self.__delete_asset_from_server(asset_id)

        self.__delete_image_from_shelve(file)

    def __delete_asset_from_server(self, asset_id):
        payload = json.dumps({"force": True, "ids": [asset_id]})
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'x-api-key': self.__apiKey,
        }
        try:
            response = requests.request("DELETE", self.__immichHost + "/assets", headers=headers, data=payload)
        except Exception as e:
            print("error when deleting asset: " + str(e))
        else:
            print(f"server responded with status {response.status_code}")

    def delete_all_uploads(self):
        try:
            with shelve.open(self.__shelve_path, flag='r') as db:
                asset_ids = [value[0] for value in db.values()]
        except dbm.error:
            asset_ids = []

        print(f"deleting {len(asset_ids)} asset(s) previously uploaded by this client")

        if asset_ids:
            payload = json.dumps({
                "force": True,
                "ids": asset_ids,
            })
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'x-api-key': self.__apiKey
            }
            try:
                response = requests.request("DELETE", self.__immichHost + "/assets", headers=headers, data=payload)
            except Exception as e:
                print("error when deleting assets: " + str(e))
            else:
                print(f"server responded with status {response.status_code}")

        with shelve.open(self.__shelve_path, flag='n'):
            pass
        print("local shelve cache cleared")

    def move(self, source, destination):
        asset_id = self.__get_image_id(source)
        self.__delete_image_from_shelve(source)
        self.__save_image_to_shelve(asset_id, destination)

    def __create_album(self, album_name):
        payload = json.dumps({
            "albumName": album_name,
            "description": "The Immich Desktop Client puts all images from " + album_name + " in this folder",
        })
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'x-api-key': self.__apiKey
        }
        response = requests.request("POST", self.__immichHost + "/albums", headers=headers, data=payload)
        print("Successfully created album " + str(response.json()))
        return json.loads(response.text)['id']

    def __get_or_create_album(self, album_name):
        if album_name in self.__album_cache:
            return self.__album_cache[album_name]

        headers = {
            'Accept': 'application/json',
            'x-api-key': self.__apiKey
        }

        response = requests.request("GET", self.__immichHost + "/albums", headers=headers)
        response = json.loads(response.text)

        album_id = None
        for album in response:
            if album['albumName'] == album_name:
                album_id = album['id']
        if album_id is None:
            print(f"no album found for '{album_name}' ... creating new one")
            album_id = self.__create_album(album_name)

        self.__album_cache[album_name] = album_id
        return album_id

    def __add_asset_to_album(self, asset_id, album_id):
        payload = json.dumps({
            "ids": [
                str(asset_id)
            ]
        })
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'x-api-key': self.__apiKey
        }

        response = requests.request("PUT", self.__immichHost + "/albums/" + album_id + "/assets",
                                    headers=headers, data=payload)
        print(response.json())
        print("successfully added asset to album")

    def __save_image_to_shelve(self, asset_id, file):
        with shelve.open(self.__shelve_path, flag='c', writeback=True) as images:
            images[file] = [asset_id, self.__get_sha1(file)]
            print("added to shelve: " + str(file) + str(images[file]))

    def __get_image_id(self, file):
        with shelve.open(self.__shelve_path, flag='r') as images:
            image_id = images[file][0]
            return image_id

    def __get_image_id_and_sha1(self, file):
        with shelve.open(self.__shelve_path, flag='r') as images:
            return images[file]

    def __delete_image_from_shelve(self, file):
        with shelve.open(self.__shelve_path, flag='c', writeback=True) as images:
            del images[file]

    def print_shelve(self):
        try:
            with shelve.open(self.__shelve_path, flag='r') as db:
                data = db.keys()

                print("Start of stored data")
                for key in data:
                    print(key, db[key])
                print("End of stored data")
        except dbm.error:
            print("cant export non-existing shelve")

    @staticmethod
    def __get_sha1(file: str):
        for i in range(0, 3):
            try:
                with open(file, 'rb', buffering=0) as f:
                    # noinspection PyTypeChecker
                    return hashlib.file_digest(f, 'sha1').hexdigest()
            except Exception as e:
                print(e)
                sleep(0.5)

    @staticmethod
    def __get_file_stats(file: str):
        # when downloading images via the browser sometimes os.stat() fails therefore it retries for 3 times
        for i in range(0, 3):
            try:
                return os.stat(file)
            except FileNotFoundError:
                sleep(0.5)
        else:
            print("Error: could not get file stats since could not find file")
            raise FileNotFoundError

    @staticmethod
    def __get_uuid():
        return str(subprocess.check_output('wmic csproduct get uuid')).split('\\r\\n')[1].strip('\\r').strip()

    def test_connection(self):
        headers = {
            'Accept': 'application/json',
            'x-api-key': self.__apiKey
        }

        try:
            response = requests.request("POST", self.__immichHost + "/auth/validateToken", headers=headers)
            print(response.json())
            return response.status_code
        except requests.exceptions.RequestException as e:
            print(e)

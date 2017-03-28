#!/usr/bin/env python3
#
# Copyright (C) 2016,2017 The University of Sheffield, UK
# 
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import os
import sys
import glob
import re
import requests
from time import sleep
from random import randint
import datetime
from ExtensionCrawler.config import *
from ExtensionCrawler.util import *
from ExtensionCrawler.archive import *
import dateutil
import dateutil.parser
from multiprocessing import Pool
from functools import partial
import shutil
import tarfile
from fs.tarfs import ReadTarFS


class Error(Exception):
    pass


class CrawlError(Error):
    def __init__(self, extid, message, pagecontent=""):
        self.extid = extid
        self.message = message
        self.pagecontent = pagecontent


class RequestResult:
    def __init__(self, response=None, exception=None):
        if response is not None:
            self.http_status = response.status_code
        self.exception = exception

    def is_ok(self):
        return (self.exception is None) and (self.http_status == 200)

    def not_authorized(self):
        return (self.exception is None) and (self.http_status == 401)

    def not_found(self):
        return (self.exception is None) and (self.http_status == 404)

    def has_exception(self):
        return self.exception is not None

    def not_available(self):
        return (self.exception is None) and (self.http_status == 503)

    def not_modified(self):
        return ((self.exception is None) and (self.http_status == 304))


class UpdateResult:
    def __init__(self, id, is_new, exception, res_overview, res_crx,
                 res_reviews, res_support):
        self.id = id
        self.new = is_new
        self.exception = exception
        self.res_overview = res_overview
        self.res_crx = res_crx
        self.res_reviews = res_reviews
        self.res_support = res_support

    def is_new(self):
        return self.new

    def is_ok(self):
        return (self.res_overview.is_ok() and
                (self.res_crx.is_ok() or self.res_crx.not_modified()) and
                ((self.res_reviews is None) or self.res_reviews.is_ok()) and (
                    (self.res_support is None) or self.res_support.is_ok()))

    def not_authorized(self):
        return (self.res_overview.not_authorized() or
                self.res_crx.not_authorized() or
                (self.res_reviews is not None and
                 self.res_reviews.not_authorized()) or (
                     self.res_support is not None and
                     self.res_support.not_authorized()))

    def not_in_store(self):
        return (
            self.res_overview.not_found() or self.res_crx.not_found() or
            (self.res_reviews is not None and self.res_reviews.not_found()) or
            (self.res_support is not None and self.res_support.not_found()))

    def has_exception(self):
        return (
            self.res_overview.has_exception() or
            self.res_crx.has_exception() or
            (self.res_reviews is not None and self.res_reviews.has_exception())
            or (self.res_support is not None and
                self.res_support.has_exception()))

    def raised_google_ddos(self):
        return (
            (self.res_reviews is not None and self.res_reviews.not_available())
            or (self.res_support is not None and
                self.res_support.not_available()))

    def not_modified(self):
        return self.res_crx.not_modified()

    def corrupt_tar(self):
        return self.exception is not None


def get_local_archive_dir(id):
    return "{}".format(id[:3])


def write_text(tar, date, fname, text):
    dir = os.path.join(os.path.splitext(tar)[0], date)
    os.makedirs(dir, exist_ok=True)
    with open(os.path.join(dir, fname), 'w') as f:
        f.write(text)


def store_request_metadata(tar, date, fname, request):
    write_text(tar, date, fname + ".headers", str(request.headers))
    write_text(tar, date, fname + ".status", str(request.status_code))
    write_text(tar, date, fname + ".url", str(request.url))


def store_request_text(tar, date, fname, request):
    write_text(tar, date, fname, request.text)
    store_request_metadata(tar, date, fname, request)


def httpdate(dt):
    weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.weekday()]
    month = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
        "Nov", "Dec"
    ][dt.month - 1]
    return "%s, %02d %s %04d %02d:%02d:%02d GMT" % (
        weekday, dt.day, month, dt.year, dt.hour, dt.minute, dt.second)


def last_modified_utc_date(path):
    if path is "":
        return ""
    return os.path.split(os.path.dirname(path))[1]


def last_modified_http_date(path):
    if path is "":
        return ""
    return httpdate(dateutil.parser.parse(last_modified_utc_date(path)))


def last_crx(archivedir, extid):
    last_crx = ""
    tar = os.path.join(archivedir, get_local_archive_dir(extid),
                       extid + ".tar")
    if os.path.exists(tar):
        archive = ReadTarFS(tar)
        old_crxs = sorted(list(archive.walk.files(filter=['*.crx'])))
        if old_crxs != []:
            last_crx = old_crxs[-1]
    return last_crx


def update_overview(tar, date, verbose, ext_id):
    logtxt = logmsg(verbose, "", "           * overview page: ")
    res = None
    try:
        res = requests.get(const_overview_url(ext_id), timeout=10)
        logtxt = logmsg(verbose, logtxt, "{}".format(str(res.status_code)))
        store_request_text(tar, date, 'overview.html', res)
    except Exception as e:
        logtxt = logmsg(verbose, logtxt, " / Exception: {}\n".format(str(e)))
        write_text(tar, date, 'overview.html.exception', str(e))
        return RequestResult(res, e), logtxt
    logtxt = logmsg(verbose, logtxt, "\n")
    return RequestResult(res), logtxt


def validate_crx_response(res, extid, extfilename):
    regex_extfilename = re.compile(r'^extension[_0-9]+\.crx$')
    if not 'Content-Type' in res.headers:
        raise CrawlError(extid, 'Did not find Content-Type header.',
                         '\n'.join(res.iter_lines()))
    if not res.headers['Content-Type'] == 'application/x-chrome-extension':
        text = [line.decode('utf-8') for line in res.iter_lines()]
        raise CrawlError(
            extid,
            'Expected Content-Type header to be application/x-chrome-extension, but got {}.'.
            format(res.headers['Content-Type']), '\n'.join(text))
    if not regex_extfilename.match(extfilename):
        raise CrawlError(
            extid, '{} is not a valid extension file name, skipping...'.format(
                extfilename))


def update_crx(archive_dir, verbose, ext_id, date):
    res = None
    extfilename = "default_ext_archive.crx"
    last_crx_file = last_crx(archive_dir, ext_id)
    last_crx_http_date = last_modified_http_date(last_crx_file)
    logtxt = logmsg(verbose, "",
                    "           * crx archive (Last: {}):   ".format(
                        valueOf(last_crx_http_date, "n/a")))
    headers = ""
    tar = os.path.join(archive_dir,
                       get_local_archive_dir(ext_id), ext_id + ".tar")
    dir = os.path.join(os.path.splitext(tar)[0], date)
    if last_crx_file is not "":
        headers = {'If-Modified-Since': last_crx_http_date}
    try:
        res = requests.get(const_download_url().format(ext_id),
                           stream=True,
                           headers=headers,
                           timeout=10)
        logtxt = logmsg(verbose, logtxt, "{}".format(str(res.status_code)))
        extfilename = os.path.basename(res.url)
        if re.search('&', extfilename):
            extfilename = "default.crx"

        store_request_metadata(tar, date, extfilename, res)

        if res.status_code == 304:
            write_text(tar, date, extfilename + ".link",
                       os.path.join("..",
                                    last_modified_utc_date(last_crx_file),
                                    extfilename) + "\n")
        elif res.status_code == 200:
            validate_crx_response(res, ext_id, extfilename)
            with open(os.path.join(dir, extfilename), 'wb') as f:
                for chunk in res.iter_content(chunk_size=512 * 1024):
                    if chunk:  # filter out keep-alive new chunks
                        f.write(chunk)
    except Exception as e:
        logtxt = logmsg(verbose, logtxt, " / Exception: {}\n".format(str(e)))
        write_text(tar, date, extfilename + ".exception", str(e))
        return RequestResult(res, e), logtxt
    logtxt = logmsg(verbose, logtxt, "\n")
    return RequestResult(res), logtxt


def update_reviews(tar, date, verbose, ext_id):
    dir = os.path.join(os.path.splitext(tar)[0], date)
    logtxt = logmsg(verbose, "", "           * review page:   ")
    res = None
    try:
        google_dos_protection()
        res = requests.post(
            const_review_url(),
            data=const_review_payload(ext_id, "0", "100"),
            timeout=10)
        logtxt = logmsg(verbose, logtxt, "{}/".format(str(res.status_code)))
        store_request_text(tar, date, 'reviews000-099.text', res)
        google_dos_protection()
        res = requests.post(
            const_review_url(),
            data=const_review_payload(ext_id, "0", "100"),
            timeout=10)
        logtxt = logmsg(verbose, logtxt, "{}".format(str(res.status_code)))
        store_request_text(tar, date, 'reviews100-199.text', res)
    except Exception as e:
        logtxt = logmsg(verbose, logtxt, " / Exception: {}\n".format(str(e)))
        write_text(tar, date, 'reviews.html.exception', str(e))
        return RequestResult(res, e), logtxt
    logtxt = logmsg(verbose, logtxt, "\n")
    return RequestResult(res), logtxt


def update_support(tar, date, verbose, ext_id):
    dir = os.path.join(os.path.splitext(tar)[0], date)
    logtxt = logmsg(verbose, "", "           * support page:  ")
    res = None
    try:
        google_dos_protection()
        res = requests.post(
            const_support_url(),
            data=const_support_payload(ext_id, "0", "100"),
            timeout=10)
        logtxt = logmsg(verbose, logtxt, "{}/".format(str(res.status_code)))
        store_request_text(tar, date, 'support000-099.text', res)
        google_dos_protection()
        res = requests.post(
            const_support_url(),
            data=const_support_payload(ext_id, "100", "100"),
            timeout=10)
        logtxt = logmsg(verbose, logtxt, "{}".format(str(res.status_code)))
        store_request_text(tar, date, 'support100-199.text', res)
    except Exception as e:
        logtxt = logmsg(verbose, logtxt, " / Exception: {}\n".format(str(e)))
        write_text(tar, date, 'support.html.exception', str(e))
        return RequestResult(res, e), logtxt
    logtxt = logmsg(verbose, logtxt, "\n")
    return RequestResult(res), logtxt


def update_extension(archivedir, verbose, forums, ext_id):
    logtxt = logmsg(verbose, "", "    Updating {}".format(ext_id))
    is_new = False
    tar_exception = None

    if forums:
        logtxt = logmsg(verbose, logtxt, " (including forums)")
    logtxt = logmsg(verbose, logtxt, "\n")
    date = datetime.datetime.now(datetime.timezone.utc).isoformat()

    tardir = os.path.join(archivedir, get_local_archive_dir(ext_id), ext_id)
    tar = (tardir + ".tar")
    if not os.path.exists(tar):
        is_new = True
    else:
        os.sync()
        shutil.rmtree(path=tardir, ignore_errors=True)
        try:
            ar = tarfile.open(tar)
            ar.extractall(path=os.path.join(archivedir,
                                            get_local_archive_dir(ext_id)))
            ar.close
        except Exception as e:
            logtxt = logmsg(
                verbose, logtxt,
                "           * FATAL: tar file corrupt (during unarchiving)")
            logtxt = logmsg(verbose, logtxt,
                            " / Exception: {}\n".format(str(e)))
            tar_exception = e
            try:
                shutil.move(tar, tardir + ".corrupt." + date + ".tar")
                write_text(tar, date,
                           ext_id + ".corrupt." + date + ".exception", str(e))
            except Exception:
                pass

    os.makedirs(
        os.path.join(archivedir, get_local_archive_dir(ext_id), ext_id),
        exist_ok=True)
    res_overview, msg_overview = update_overview(tar, date, verbose, ext_id)
    res_crx, msg_crx = update_crx(archivedir, verbose, ext_id, date)
    res_reviews = None
    msg_reviews = ""
    res_support = None
    msg_support = ""
    if forums:
        res_reviews, msg_reviews = update_reviews(tar, date, verbose, ext_id)
        res_support, msg_support = update_support(tar, date, verbose, ext_id)
    log(verbose, logtxt + msg_overview + msg_crx + msg_reviews + msg_support)

    try:
        os.sync()
        if os.path.exists(tardir + "bak.tar"):
            shutil.move(tardir + ".bak.tar", tardir + ".bak." + date + ".tar")
            os.remove(tardir + ".bak." + date + ".tar")
    except Exception:
        pass

    try:
        if os.path.exists(tar):
            shutil.move(tar, tardir + ".bak.tar")
    except Exception as e:
        logtxt = logmsg(verbose, logtxt,
                        "           * FATAL: cannot rename old tar archive")
        logtxt = logmsg(verbose, logtxt, " / Exception: {}\n".format(str(e)))
        tar_exception = e
        try:
            write_text(tar, date, ext_id + ".tar.rename.exception", str(e))
        except Exception:
            pass
    try:
        ar = tarfile.open(tar, mode='w')
        ar.add(tardir, arcname=ext_id)
        ar.close()
    except Exception as e:
        logtxt = logmsg(verbose, logtxt,
                        "           * FATAL: cannot create tar archive")
        logtxt = logmsg(verbose, logtxt, " / Exception: {}\n".format(str(e)))
        tar_exception = e
        try:
            write_text(tar, date, ext_id + ".tar.create.exception", str(e))
        except Exception:
            pass
    try:
        shutil.rmtree(path=os.path.join(archivedir,
                                        get_local_archive_dir(ext_id), ext_id))
    except Exception as e:
        logtxt = logmsg(verbose, logtxt,
                        "           * FATAL: cannot remove archive directory")
        logtxt = logmsg(verbose, logtxt, " / Exception: {}\n".format(str(e)))
        tar_exception = e
        try:
            write_text(tar, date, ext_id + ".dir.remove.exception", str(e))
        except Exception:
            pass

    return UpdateResult(ext_id, is_new, tar_exception, res_overview, res_crx,
                        res_reviews, res_support)


def update_extensions(archivedir, verbose, forums_ext_ids, ext_ids):
    ext_with_forums = []
    ext_without_forums = []
    ext_ids = list(set(ext_ids) - set(forums_ext_ids))
    forums_ext_ids = list(set(forums_ext_ids))
    log(verbose, "Updating {} extensions ({} including forums)\n".format(
        len(ext_ids), len(forums_ext_ids)))
    # First, update extensions with forums sequentially (and with delays) to
    # avoid running into Googles DDOS detection. 
    log(verbose,
        "  Updating {} extensions including forums (sequentially))\n".format(
            len(forums_ext_ids)))

    ext_with_forums = list(
        map(
            partial(update_extension, archivedir, verbose, True),
            forums_ext_ids))

    # Second, update extensions without forums parallel to increase speed.
    parallel_ids = list(set(ext_ids) - set(forums_ext_ids))
    log(verbose,
        "  Updating {} extensions excluding forums (parallel))\n".format(
            len(parallel_ids)))
    with Pool(24) as p:
        ext_without_forums = list(
            p.map(
                partial(update_extension, archivedir, verbose, False),
                parallel_ids))

    return ext_with_forums + ext_without_forums


def get_existing_ids(archivedir, verbose):
    byte = '[0-9a-z][0-9a-z][0-9a-z][0-9a-z][0-9a-z][0-9a-z][0-9a-z][0-9a-z]'
    word = byte + byte + byte + byte
    return list(
        map(lambda d: re.sub(".tar$", "", re.sub("^.*\/", "", d)),
            glob.glob(os.path.join(archivedir, "*", word + ".tar"))))


def get_forum_ext_ids(confdir, verbose):
    with open(os.path.join(confdir, "forums.conf")) as f:
        ids = f.readlines()
    ids = [x.strip() for x in ids]
    return ids

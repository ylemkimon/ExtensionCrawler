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

import sys
import xml.etree.ElementTree as ET
import requests
import re
from functools import reduce
import ExtensionCrawler.config


def crawl_nearly_all_of_ext_ids():
    def get_inner_elems(doc):
        return ET.fromstring(doc).findall(".//{{{}}}loc".format(
            ExtensionCrawler.config.const_sitemap_scheme()))

    def is_generic_url(url):
        return re.match("^{}\?shard=\d+&numshards=\d+$".format(
            ExtensionCrawler.config.const_sitemap_url()), url)

    shard_elems = get_inner_elems(
        requests.get(ExtensionCrawler.config.const_sitemap_url()).text)
    shard_urls = list(
        filter(is_generic_url, ([elem.text for elem in shard_elems])))
    shards = list(map(lambda u: requests.get(u).text, shard_urls))

    overview_urls = reduce(
        lambda x, y: x + y,
        map(lambda s: [elem.text for elem in get_inner_elems(s)], shards), [])
    return [re.search("[a-z]{32}", url).group(0) for url in overview_urls]

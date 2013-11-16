#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai

# The MIT License (MIT)

# Copyright (c) 2013 Casey Duquette

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""  """

from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

# Add the calibre submodule to the path
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'calibre', 'src'))

import socket, re, datetime
from collections import OrderedDict
from threading import Thread

from lxml.html import fromstring, tostring

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library.comments import sanitize_comments_html
from calibre.utils.cleantext import clean_ascii_chars
from calibre.utils.localization import canonicalize_lang

import calibre_plugins.shelfari.config as cfg

__author__ = "Casey Duquette"
__copyright__ = "Copyright 2013"
__credits__ = ["Grant Drake <grant.drake@gmail.com>"]

__license__ = "MIT"
__version__ = ""
__maintainer__ = "Casey Duquette"
__email__ = ""
__url__ = "https://github.com/beeftornado/calibre-shelfari-metadata"


class Worker(Thread): # Get details

    '''
    Get book details from Shelfari book page in a separate thread
    '''

    def __init__(self, url, result_queue, browser, log, relevance, plugin, timeout=20):
        Thread.__init__(self)
        self.daemon = True
        self.url, self.result_queue = url, result_queue
        self.log, self.timeout = log, timeout
        self.relevance, self.plugin = relevance, plugin
        self.browser = browser.clone_browser()
        self.cover_url = self.shelfari_id = self.isbn = None

        lm = {
                'eng': ('English', 'Englisch'),
                'fra': ('French', 'Français'),
                'ita': ('Italian', 'Italiano'),
                'dut': ('Dutch',),
                'deu': ('German', 'Deutsch'),
                'spa': ('Spanish', 'Espa\xf1ol', 'Espaniol'),
                'jpn': ('Japanese', u'日本語'),
                'por': ('Portuguese', 'Português'),
                }
        self.lang_map = {}
        for code, names in lm.iteritems():
            for name in names:
                self.lang_map[name] = code

    def run(self):
        try:
            self.get_details()
        except:
            self.log.exception('get_details failed for url: %r'%self.url)

    def get_details(self):
        try:
            self.log.info('Shelfari book url: %r'%self.url)
            raw = self.browser.open_novisit(self.url, timeout=self.timeout).read().strip()
        except Exception as e:
            if callable(getattr(e, 'getcode', None)) and \
                    e.getcode() == 404:
                self.log.error('URL malformed: %r'%self.url)
                return
            attr = getattr(e, 'args', [None])
            attr = attr if attr else [None]
            if isinstance(attr[0], socket.timeout):
                msg = 'Shelfari timed out. Try again later.'
                self.log.error(msg)
            else:
                msg = 'Failed to make details query: %r'%self.url
                self.log.exception(msg)
            return

        raw = raw.decode('utf-8', errors='replace')
        #open('c:\\shelfari.html', 'wb').write(raw)

        if '<title>404 - ' in raw:
            self.log.error('URL malformed: %r'%self.url)
            return

        try:
            root = fromstring(clean_ascii_chars(raw))
        except:
            msg = 'Failed to parse shelfari details page: %r'%self.url
            self.log.exception(msg)
            return

        try:
            # Look at the <title> attribute for page to make sure that we were actually returned
            # a details page for a book. If the user had specified an invalid ISBN, then the results
            # page will just do a textual search.
            title_node = root.xpath('//title')
            if title_node:
                page_title = title_node[0].text_content().strip()
                if page_title is None:
                    self.log.error('Failed to see search results in page title: %r'%self.url)
                    return
        except:
            msg = 'Failed to read shelfari page title: %r'%self.url
            self.log.exception(msg)
            return

        errmsg = root.xpath('//*[@id="errorMessage"]')
        if errmsg:
            msg = 'Failed to parse shelfari details page: %r'%self.url
            msg += tostring(errmsg, method='text', encoding=unicode).strip()
            self.log.error(msg)
            return

        self.parse_details(root)

    def parse_details(self, root):
        try:
            shelfari_id = self.parse_shelfari_id(self.url)
        except:
            self.log.exception('Error parsing shelfari id for url: %r'%self.url)
            shelfari_id = None

        try:
            (title, series, series_index) = self.parse_title_series(root)
        except:
            self.log.exception('Error parsing title and series for url: %r'%self.url)
            title = series = series_index = None

        try:
            authors = self.parse_authors(root)
        except:
            self.log.exception('Error parsing authors for url: %r'%self.url)
            authors = []

        if not title or not authors or not shelfari_id:
            self.log.error('Could not find title/authors/shelfari id for %r'%self.url)
            self.log.error('Shelfari: %r Title: %r Authors: %r'%(shelfari_id, title,
                authors))
            return

        mi = Metadata(title, authors)
        if series:
            mi.series = series
            mi.series_index = series_index
        mi.set_identifier('shelfari', shelfari_id)
        self.shelfari_id = shelfari_id

        try:
            isbn = self.parse_isbn(root)
            if isbn:
                self.isbn = mi.isbn = isbn
        except:
            self.log.exception('Error parsing ISBN for url: %r'%self.url)

        try:
            mi.rating = self.parse_rating(root)
        except:
            self.log.exception('Error parsing ratings for url: %r'%self.url)

        try:
            mi.comments = self.parse_comments(root)
        except:
            self.log.exception('Error parsing comments for url: %r'%self.url)

        try:
            self.cover_url = self.parse_cover(root)
        except:
            self.log.exception('Error parsing cover for url: %r'%self.url)
        mi.has_cover = bool(self.cover_url)

        try:
            tags = self.parse_tags(root)
            if tags:
                mi.tags = tags
        except:
            self.log.exception('Error parsing tags for url: %r'%self.url)

        try:
            mi.publisher, mi.pubdate = self.parse_publisher_and_date(root)
        except:
            self.log.exception('Error parsing publisher and date for url: %r'%self.url)

        try:
            lang = self._parse_language(root)
            if lang:
                mi.language = lang
        except:
            self.log.exception('Error parsing language for url: %r'%self.url)

        mi.source_relevance = self.relevance

        if self.shelfari_id:
            if self.isbn:
                self.plugin.cache_isbn_to_identifier(self.isbn, self.shelfari_id)
            if self.cover_url:
                self.plugin.cache_identifier_to_cover_url(self.shelfari_id,
                        self.cover_url)

        self.plugin.clean_downloaded_metadata(mi)

        self.result_queue.put(mi)

    def parse_shelfari_id(self, url):
        return re.search('/books/(\d+)', url).groups(0)[0]

    def parse_title_series(self, root):
        # Default values
        title_text, series_text, book_year, book_number = (None,)*4
        
        # Get the title from the source
        title_node = root.xpath('//h1[@class="hover_title"]')
        if not title_node:
            return (None, None, None)
        title_text = title_node[0].text_content().strip()
        
        # The book title may have a year in it, we can split that out
        match = re.search('\((\d{4})\)$', title_text)
        if match:
            book_year = match.groups(0)
            title_text = re.sub('\((\d{4})\)$', '', title_text).strip()
            
        # Find the series if the book is a part of one
        series_node = root.xpath('//span[@class="series"]')
        if not series_node:
            return (title_text, None, None)
        series_text = series_node[0].text_content().strip()
        
        # Series text may or may not have a book number, let's see if it's there
        match = re.search(': Book (\d+)', series_text)
        if match:
            book_number = match.groups(0)
            series_text = re.sub(': Book (\d+)', '', series_text).strip()
        
        return (title_text, series_text, book_number)

    def parse_authors(self, root):
        # Build a dict of authors with their contribution if any in values
        div_authors = root.xpath('//div[@id="WikiModule_Contributors"]//ol/li')
        if not div_authors:
            return
        authors = []
        for li in div_authors:
            li_text = li.text_content()
            author = re.sub('\s*\([\w\s]*\)', '', li_text).strip()
            authors.append(author)
        return authors

    def parse_rating(self, root):
        rating_node = root.xpath('//ul[@class=rating]/li[@class="current"]')
        if rating_node:
            rating_text = rating_node[0].text_content()
            rating_text = re.sub('[^0-9]', '', rating_text)
            rating_value = float(rating_text)
            if rating_value >= 100:
                return rating_value / 100
            return rating_value

    def parse_comments(self, root):
        description_node = root.xpath('//div[@class="ugc nonTruncatedSum"]/p')
        if description_node:
            desc = description_node[0]
            comments = tostring(desc, method='html', encoding=unicode).strip()
            while comments.find('  ') >= 0:
                comments = comments.replace('  ',' ')
            comments = sanitize_comments_html(comments)
            return comments

    def parse_cover(self, root):
        imgcol_node = root.xpath('//div[@id="BookMasterImage"]//img/@src')
        if imgcol_node:
            img_url = imgcol_node[0]
            return img_url

    def parse_isbn(self, root):
        isbn_node = root.xpath('//acronym[@title="International Standard Book Number"]')
        if isbn_node:
            isbn_text = isbn_node[0].text_content()
            isbn_text = re.sub('[^0-9]', '', isbn_text).strip()
            return isbn_text

    def parse_publisher_and_date(self, root):
        publisher = None
        pub_date = None
        edition_node = root.xpath('//div[@id="WikiModule_FirstEdition"]//div')
        if edition_node:
            for div in edition_node:
                if 'Publisher' in div.text_content():
                    match = re.search('Publisher: ([\w\s]+)', div).strip()
                    if match:
                        publisher = match.groups(0).strip()
            
                if None and pubdate_text:
                    pub_date = self._convert_date_text(pubdate_text)
        return (publisher, pub_date)

    def parse_tags(self, root):
        return None
        # Shelfari does not have "tags", but it does have Genres (wrapper around popular shelves)
        # We will use those as tags (with a bit of massaging)
        genres_node = root.xpath('//div[@class="stacked"]/div/div/div[contains(@class, "bigBoxContent")]/div/div[@class="left"]')
        #self.log.info("Parsing tags")
        if genres_node:
            #self.log.info("Found genres_node")
            genre_tags = list()
            for genre_node in genres_node:
                sub_genre_nodes = genre_node.xpath('a')
                genre_tags_list = [sgn.text_content().strip() for sgn in sub_genre_nodes]
                #self.log.info("Found genres_tags list:", genre_tags_list)
                if genre_tags_list:
                    genre_tags.append(' > '.join(genre_tags_list))
            calibre_tags = self._convert_genres_to_calibre_tags(genre_tags)
            if len(calibre_tags) > 0:
                return calibre_tags

    def _convert_genres_to_calibre_tags(self, genre_tags):
        # for each tag, add if we have a dictionary lookup
        calibre_tag_lookup = cfg.plugin_prefs[cfg.STORE_NAME][cfg.KEY_GENRE_MAPPINGS]
        calibre_tag_map = dict((k.lower(),v) for (k,v) in calibre_tag_lookup.iteritems())
        tags_to_add = list()
        for genre_tag in genre_tags:
            tags = calibre_tag_map.get(genre_tag.lower(), None)
            if tags:
                for tag in tags:
                    if tag not in tags_to_add:
                        tags_to_add.append(tag)
        return list(tags_to_add)

    def _convert_date_text(self, date_text):
        # Note that the date text could be "2003", "December 2003" or "December 10th 2003"
        year = int(date_text[-4:])
        month = 1
        day = 1
        if len(date_text) > 4:
            text_parts = date_text[:len(date_text)-5].partition(' ')
            month_name = text_parts[0]
            # Need to convert the month name into a numeric value
            # For now I am "assuming" the Shelfari website only displays in English
            # If it doesn't will just fallback to assuming January
            month_dict = {"January":1, "February":2, "March":3, "April":4, "May":5, "June":6,
                "July":7, "August":8, "September":9, "October":10, "November":11, "December":12}
            month = month_dict.get(month_name, 1)
            if len(text_parts[2]) > 0:
                day = int(re.match('([0-9]+)', text_parts[2]).groups(0)[0])
        from calibre.utils.date import utc_tz
        return datetime.datetime(year, month, day, tzinfo=utc_tz)

    def _parse_language(self, root):
        lang_node = root.xpath('//div[@id="metacol"]/div[@id="details"]/div[@class="buttons"]/div[@id="bookDataBox"]/div/div[@itemprop="inLanguage"]')
        if lang_node:
            raw = tostring(lang_node[0], method='text', encoding=unicode).strip()
            ans = self.lang_map.get(raw, None)
            if ans:
                return ans
            ans = canonicalize_lang(ans)
            if ans:
                return ans

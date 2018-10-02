import logging
import os
import re
import zipfile
from typing import Dict, Set, Pattern

import xml.etree.ElementTree as ET  # type: ignore

from .archive import ArchiveBasedAbstractParser

# pylint: disable=line-too-long

# Make pyflakes happy
assert Set
assert Pattern

def _parse_xml(full_path: str):
    """ This function parses XML, with namespace support. """

    namespace_map = dict()
    for _, (key, value) in ET.iterparse(full_path, ("start-ns", )):
        # The ns[0-9]+ namespaces are reserved for internal usage, so
        # we have to use an other nomenclature.
        if re.match('^ns[0-9]+$', key, re.I):  # pragma: no cover
            key = 'mat' + key[2:]

        namespace_map[key] = value
        ET.register_namespace(key, value)

    return ET.parse(full_path), namespace_map


def _sort_xml_attributes(full_path: str) -> bool:
    """ Sort xml attributes lexicographically,
    because it's possible to fingerprint producers (MS Office, Libreoffice, …)
    since they are all using different orders.
    """
    tree = ET.parse(full_path)

    for c in tree.getroot():
        c[:] = sorted(c, key=lambda child: (child.tag, child.get('desc')))

    tree.write(full_path, xml_declaration=True)
    return True


class MSOfficeParser(ArchiveBasedAbstractParser):
    mimetypes = {
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation'
    }
    files_to_keep = {
        '[Content_Types].xml',
        '_rels/.rels',
        'word/_rels/document.xml.rels',
        'word/document.xml',
        'word/fontTable.xml',
        'word/settings.xml',
        'word/styles.xml',
        'docProps/app.xml',
        'docProps/core.xml',

        # https://msdn.microsoft.com/en-us/library/dd908153(v=office.12).aspx
        'word/stylesWithEffects.xml',
    }
    files_to_omit = set(map(re.compile, {  # type: ignore
        'word/webSettings.xml',
        'word/theme',
    }))

    @staticmethod
    def __remove_rsid(full_path: str) -> bool:
        """ The method will remove "revision session ID".  We're '}rsid'
        instead of proper parsing, since rsid can have multiple forms, like
        `rsidRDefault`, `rsidR`, `rsids`, …

        We're removing rsid tags in two times, because we can't modify
        the xml while we're iterating on it.

        For more details, see
        - https://msdn.microsoft.com/en-us/library/office/documentformat.openxml.wordprocessing.previoussectionproperties.rsidrpr.aspx
        - https://blogs.msdn.microsoft.com/brian_jones/2006/12/11/whats-up-with-all-those-rsids/
        """
        try:
            tree, namespace = _parse_xml(full_path)
        except ET.ParseError:
            return False

        # rsid, tags or attributes, are always under the `w` namespace
        if 'w' not in namespace.keys():
            return True

        parent_map = {c:p for p in tree.iter() for c in p}

        elements_to_remove = list()
        for item in tree.iterfind('.//', namespace):
            if '}rsid' in item.tag.strip().lower():  # rsid as tag
                elements_to_remove.append(item)
                continue
            for key in list(item.attrib.keys()):  # rsid as attribute
                if '}rsid' in key.lower():
                    del item.attrib[key]

        for element in elements_to_remove:
            parent_map[element].remove(element)

        tree.write(full_path, xml_declaration=True)
        return True

    @staticmethod
    def __remove_revisions(full_path: str) -> bool:
        """ In this function, we're changing the XML document in several
        different times, since we don't want to change the tree we're currently
        iterating on.
        """
        try:
            tree, namespace = _parse_xml(full_path)
        except ET.ParseError as e:
            logging.error("Unable to parse %s: %s", full_path, e)
            return False

        # Revisions are either deletions (`w:del`) or
        # insertions (`w:ins`)
        del_presence = tree.find('.//w:del', namespace)
        ins_presence = tree.find('.//w:ins', namespace)
        if del_presence is None and ins_presence is None:
            return True  # No revisions are present

        parent_map = {c:p for p in tree.iter() for c in p}

        elements = list()
        for element in tree.iterfind('.//w:del', namespace):
            elements.append(element)
        for element in elements:
            parent_map[element].remove(element)

        elements = list()
        for element in tree.iterfind('.//w:ins', namespace):
            for position, item in enumerate(tree.iter()):  # pragma: no cover
                if item == element:
                    for children in element.iterfind('./*'):
                        elements.append((element, position, children))
                    break
        for (element, position, children) in elements:
            parent_map[element].insert(position, children)
            parent_map[element].remove(element)

        tree.write(full_path, xml_declaration=True)
        return True

    def __remove_content_type_members(self, full_path: str) -> bool:
        """ The method will remove the dangling references
        form the [Content_Types].xml file, since MS office doesn't like them
        """
        try:
            tree, namespace = _parse_xml(full_path)
        except ET.ParseError:  # pragma: no cover
            return False

        if len(namespace.items()) != 1:
            return False  # there should be only one namespace for Types

        removed_fnames = set()
        with zipfile.ZipFile(self.filename) as zin:
            for fname in [item.filename for item in zin.infolist()]:
                if any(map(lambda r: r.search(fname), self.files_to_omit)):  # type: ignore
                    removed_fnames.add(fname)

        root = tree.getroot()
        for item in root.findall('{%s}Override' % namespace['']):
            name = item.attrib['PartName'][1:]  # remove the leading '/'
            if name in removed_fnames:
                root.remove(item)

        tree.write(full_path, xml_declaration=True)
        return True

    def _specific_cleanup(self, full_path: str) -> bool:
        # pylint: disable=too-many-return-statements
        if os.stat(full_path).st_size == 0:  # Don't process empty files
            return True

        if not full_path.endswith('.xml'):
            return True

        if full_path.endswith('/[Content_Types].xml'):
            # this file contains references to files that we might
            # remove, and MS Office doesn't like dangling references
            if self.__remove_content_type_members(full_path) is False:
                return False
        elif full_path.endswith('/word/document.xml'):
            # this file contains the revisions
            if self.__remove_revisions(full_path) is False:
                return False
        elif full_path.endswith('/docProps/app.xml'):
            # This file must be present and valid,
            # so we're removing as much as we can.
            with open(full_path, 'wb') as f:
                f.write(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
                f.write(b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">')
                f.write(b'</Properties>')
        elif full_path.endswith('/docProps/core.xml'):
            # This file must be present and valid,
            # so we're removing as much as we can.
            with open(full_path, 'wb') as f:
                f.write(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
                f.write(b'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties">')
                f.write(b'</cp:coreProperties>')


        if self.__remove_rsid(full_path) is False:
            return False

        try:
            _sort_xml_attributes(full_path)
        except ET.ParseError as e:  # pragma: no cover
            logging.error("Unable to parse %s: %s", full_path, e)
            return False

        # This is awful, I'm sorry.
        #
        # Microsoft Office isn't happy when we have the `mc:Ignorable`
        # tag containing namespaces that aren't present in the xml file,
        # so instead of trying to remove this specific tag with etree,
        # we're removing it, with a regexp.
        #
        # Since we're the ones producing this file, via the call to
        # _sort_xml_attributes, there won't be any "funny tricks".
        # Worst case, the tag isn't present, and everything is fine.
        #
        # see: https://docs.microsoft.com/en-us/dotnet/framework/wpf/advanced/mc-ignorable-attribute
        with open(full_path, 'rb') as f:
            text = f.read()
            out = re.sub(b'mc:Ignorable="[^"]*"', b'', text, 1)
        with open(full_path, 'wb') as f:
            f.write(out)

        return True

    def get_meta(self) -> Dict[str, str]:
        """
        Yes, I know that parsing xml with regexp ain't pretty,
        be my guest and fix it if you want.
        """
        metadata = {}
        zipin = zipfile.ZipFile(self.filename)
        for item in zipin.infolist():
            if item.filename.startswith('docProps/') and item.filename.endswith('.xml'):
                try:
                    content = zipin.read(item).decode('utf-8')
                    results = re.findall(r"<(.+)>(.+)</\1>", content, re.I|re.M)
                    for (key, value) in results:
                        metadata[key] = value
                except (TypeError, UnicodeDecodeError):  # We didn't manage to parse the xml file
                    metadata[item.filename] = 'harmful content'
            for key, value in self._get_zipinfo_meta(item).items():
                metadata[key] = value
        zipin.close()
        return metadata


class LibreOfficeParser(ArchiveBasedAbstractParser):
    mimetypes = {
        'application/vnd.oasis.opendocument.text',
        'application/vnd.oasis.opendocument.spreadsheet',
        'application/vnd.oasis.opendocument.presentation',
        'application/vnd.oasis.opendocument.graphics',
        'application/vnd.oasis.opendocument.chart',
        'application/vnd.oasis.opendocument.formula',
        'application/vnd.oasis.opendocument.image',
    }
    files_to_keep = {
        'META-INF/manifest.xml',
        'content.xml',
        'manifest.rdf',
        'mimetype',
        'settings.xml',
        'styles.xml',
    }
    files_to_omit = set(map(re.compile, {  # type: ignore
        r'^meta\.xml$',
        '^Configurations2/',
        '^Thumbnails/',
    }))

    @staticmethod
    def __remove_revisions(full_path: str) -> bool:
        try:
            tree, namespace = _parse_xml(full_path)
        except ET.ParseError as e:
            logging.error("Unable to parse %s: %s", full_path, e)
            return False

        if 'office' not in namespace.keys():  # no revisions in the current file
            return True

        for text in tree.getroot().iterfind('.//office:text', namespace):
            for changes in text.iterfind('.//text:tracked-changes', namespace):
                text.remove(changes)

        tree.write(full_path, xml_declaration=True)
        return True

    def _specific_cleanup(self, full_path: str) -> bool:
        if os.stat(full_path).st_size == 0:  # Don't process empty files
            return True

        if os.path.basename(full_path).endswith('.xml'):
            if os.path.basename(full_path) == 'content.xml':
                if self.__remove_revisions(full_path) is False:
                    return False

            try:
                _sort_xml_attributes(full_path)
            except ET.ParseError as e:
                logging.error("Unable to parse %s: %s", full_path, e)
                return False
        return True

    def get_meta(self) -> Dict[str, str]:
        """
        Yes, I know that parsing xml with regexp ain't pretty,
        be my guest and fix it if you want.
        """
        metadata = {}
        zipin = zipfile.ZipFile(self.filename)
        for item in zipin.infolist():
            if item.filename == 'meta.xml':
                try:
                    content = zipin.read(item).decode('utf-8')
                    results = re.findall(r"<((?:meta|dc|cp).+?)>(.+)</\1>", content, re.I|re.M)
                    for (key, value) in results:
                        metadata[key] = value
                except (TypeError, UnicodeDecodeError):  # We didn't manage to parse the xml file
                    metadata[item.filename] = 'harmful content'
            for key, value in self._get_zipinfo_meta(item).items():
                metadata[key] = value
        zipin.close()
        return metadata

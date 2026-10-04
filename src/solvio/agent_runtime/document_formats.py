"""Fixed additional document contracts and bounded container validation.

This is admission validation, not a document text extractor. The existing
native textutil process still performs conversion inside the unchanged offline
sandbox. No archive entry is extracted to disk and no XML reference is fetched.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import re
import stat
import struct
import zipfile
import zlib
import xml.etree.ElementTree as XML

MAX_INPUT_BYTES = 1_048_576
MAX_EXPANDED_BYTES = 4_194_304
MAX_ENTRIES = 128
MAX_RATIO = 200
MAX_XML_BYTES = 1_048_576
MAX_XML_ELEMENTS = 20_000
MAX_XML_DEPTH = 64
MAX_OUTPUT_BYTES = 65_536
_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
_OFFICE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument'
_DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
_ODT = 'application/vnd.oasis.opendocument.text'
_OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'
_MANIFEST = 'urn:oasis:names:tc:opendocument:xmlns:manifest:1.0'
_TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'


@dataclass(frozen=True)
class FormatProfile:
    format: str
    contract: str
    max_input_bytes: int
    filename: str
    native_arguments: tuple[str, ...]
    contract_digest: str


def _make(input_format, name, limit):
    arguments = ('-format', input_format, '-convert', 'txt', '-stdin', '-stdout', '-encoding', 'UTF-8')
    if input_format == 'txt':
        arguments += ('-inputencoding', 'UTF-8')
    body = {'contract': name, 'capability': 'document_extract_text', 'version': 1,
        'operation': 'extract_text', 'input_format': input_format, 'output_encoding': 'utf-8',
        'max_input_bytes': limit, 'max_output_bytes': MAX_OUTPUT_BYTES,
        'input_immutable': True, 'network': False, 'external_writes': False,
        'local_adapter_development': True, 'native_executable': '/usr/bin/textutil',
        'native_arguments': list(arguments), 'empty_text': 'refused',
        'validation': {'utf8_text': True, 'nul': False} if input_format == 'txt' else {
            'profile': 'office_zip_xml_v1', 'max_entries': MAX_ENTRIES,
            'max_expanded_bytes': MAX_EXPANDED_BYTES, 'max_ratio': MAX_RATIO,
            'max_xml_bytes': MAX_XML_BYTES, 'max_xml_elements': MAX_XML_ELEMENTS,
            'max_xml_depth': MAX_XML_DEPTH, 'encryption': False, 'archive_symlinks': False,
            'xml_entities': False, 'external_fetch': False,
            'compressed_stream': 'exact_bounded_eof_crc_v1'}}
    digest = hashlib.sha256(b'SOLVIO_DOCUMENT_CONTRACT_V1\0' + json.dumps(
        body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return FormatProfile(input_format, name, limit, 'input-document.' + input_format, arguments, digest)


PROFILES = tuple(_make(*args) for args in (
    ('txt', 'utf8_text_v1', MAX_OUTPUT_BYTES),
    ('docx', 'docx_text_v1', MAX_INPUT_BYTES), ('odt', 'odt_text_v1', MAX_INPUT_BYTES)))


def profile(input_format):
    match = next((item for item in PROFILES if item.format == input_format), None)
    if match is None:
        raise ValueError('unsupported_document_format')
    return match


def _xml(data, *, native_manifest=False, max_xml_bytes=MAX_XML_BYTES,
         max_xml_elements=MAX_XML_ELEMENTS, max_xml_depth=MAX_XML_DEPTH):
    if not data or len(data) > max_xml_bytes or b'\0' in data:
        raise ValueError('invalid_document_xml')
    try:
        decoded = data.decode('utf-8-sig')
        if native_manifest:
            # textutil emits this literal legacy ODF declaration. It carries
            # no entity declarations; strip it only in the admission parser.
            # The immutable original still reaches the offline converter.
            decoded = decoded.replace('<!DOCTYPE manifest:manifest PUBLIC '
                '"-//OpenOffice.org//DTD Manifest 1.0//EN" "Manifest.dtd">', '', 1)
        declaration = re.match(r'^<\?xml\b[^?]*\bencoding\s*=\s*[\'"]([^\'"]+)', decoded, re.I)
        if declaration and declaration.group(1).lower() not in ('utf-8', 'utf8', 'us-ascii'):
            raise ValueError('document_xml_encoding_unsupported')
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', decoded, re.I):
            raise ValueError('document_xml_entities_forbidden')
        parser = XML.XMLPullParser(events=('start', 'end'))
        depth = count = 0
        root = None
        for offset in range(0, len(decoded), 4096):
            parser.feed(decoded[offset:offset + 4096])
            for event, element in parser.read_events():
                if event == 'start':
                    depth += 1
                    count += 1
                    if root is None:
                        root = element
                    if depth > max_xml_depth or count > max_xml_elements:
                        raise ValueError('document_xml_structure_limit')
                else:
                    depth -= 1
        parser.close()
        if root is None or depth:
            raise ValueError('invalid_document_xml')
        return root
    except (UnicodeError, XML.ParseError):
        raise ValueError('invalid_document_xml') from None


def _read_entry(archive, entry, content, remaining, *,
                max_expanded_bytes=MAX_EXPANDED_BYTES, max_xml_bytes=MAX_XML_BYTES):
    """Check the actual stream, not ZipExtFile's declared-length prefix.

    ZipExtFile intentionally stops at ZipInfo.file_size. An attacker can bind
    that prefix's CRC and hide a much larger native inflate behind it. Reuse
    ZipFile.open's filename/overlap validation, but decompress its exact raw
    span with zlib's output ceiling and require real EOF with no trailing data.
    No generated adapter or native tool receives an unchecked tail.
    """
    header = struct.unpack_from('<4s5H3I2H', content, entry.header_offset)
    if (header[0] != b'PK\x03\x04' or header[2] != entry.flag_bits
            or header[3] != entry.compress_type):
        raise ValueError('document_zip_header_changed')
    expected = (entry.CRC, entry.compress_size, entry.file_size)
    if entry.flag_bits & 8:
        if any(value not in (0, wanted) for value, wanted in zip(header[6:9], expected)):
            raise ValueError('document_zip_header_changed')
    elif header[6:9] != expected:
        raise ValueError('document_zip_header_changed')
    with archive.open(entry) as checked:
        # The installed stdlib exposes the validated compressed-data start on
        # seekable ZIPs. Missing/private API changes fail closed; no fallback
        # to ZipExtFile.read's truncated view is permitted.
        start = getattr(checked, '_orig_compress_start', None)
    end = start + entry.compress_size if type(start) is int else -1
    boundary = getattr(entry, '_end_offset', None)
    if (type(start) is not int or start != entry.header_offset + 30 + header[9] + header[10]
            or type(boundary) is not int or not 0 <= start <= end <= boundary <= len(content)):
        raise ValueError('document_zip_span_invalid')
    if entry.flag_bits & 8:
        descriptor = end + (4 if content[end:end + 4] == b'PK\x07\x08' else 0)
        if descriptor + 12 > boundary or struct.unpack_from('<III', content, descriptor) != expected:
            raise ValueError('document_zip_descriptor_changed')
    raw = content[start:end]
    ceiling = min(entry.file_size, remaining,
                  max_xml_bytes if entry.filename.casefold().endswith(('.xml', '.rels')) else max_expanded_bytes)
    if entry.compress_type == zipfile.ZIP_DEFLATED:
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        data = decoder.decompress(raw, ceiling + 1)
        if not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise ValueError('document_zip_stream_invalid')
    else:
        data = raw
    if (len(data) > ceiling or len(data) != entry.file_size
            or zlib.crc32(data) != entry.CRC):
        raise ValueError('document_zip_length_or_crc_changed')
    return data


def _container(content):
    """Validate every directory entry before reading any compressed content."""
    if not content.startswith(b'PK\x03\x04') or content[-22:-18] != b'PK\x05\x06':
        raise ValueError('invalid_document_zip')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= MAX_ENTRIES or archive.comment:
                raise ValueError('document_zip_entry_limit')
            names, offsets = set(), set()
            total = 0
            for entry in entries:
                name = entry.filename
                parts = name.rstrip('/').split('/')
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if (not name or len(name) > 255 or name != entry.orig_filename
                        or any(ord(c) < 32 for c in name) or '\\' in name or ':' in name
                        or any(part in ('', '.', '..') for part in parts)
                        or name.casefold() in names or entry.header_offset in offsets
                        or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or bool(entry.flag_bits & 0x41)
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or entry.file_size < 0 or entry.compress_size < 0
                        or (entry.is_dir() and entry.file_size)
                        or (mode == stat.S_IFDIR and not entry.is_dir())
                        or entry.file_size > MAX_EXPANDED_BYTES
                        or entry.file_size > MAX_RATIO * max(1, entry.compress_size)):
                    raise ValueError('invalid_document_zip_entry')
                names.add(name.casefold())
                offsets.add(entry.header_offset)
                total += entry.file_size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError('document_zip_expansion_limit')
            files, actual_total = {}, 0
            for entry in entries:
                data = _read_entry(archive, entry, content, MAX_EXPANDED_BYTES - actual_total)
                actual_total += len(data)
                files[entry.filename] = data
            xml = {name: _xml(data, native_manifest=name == 'META-INF/manifest.xml') for name, data in files.items()
                   if name.casefold().endswith(('.xml', '.rels'))}
            return files, xml, entries
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError,
            EOFError, zlib.error, struct.error, UnicodeError):
        raise ValueError('invalid_document_zip') from None


def validate(content, input_format):
    selected = profile(input_format)
    if type(content) is not bytes or not 1 <= len(content) <= selected.max_input_bytes:
        raise ValueError('invalid_document_size')
    if input_format == 'txt':
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('document_input_not_utf8') from None
        if any(ord(c) < 32 and c not in '\t\r\n' for c in text):
            raise ValueError('invalid_document_text')
        if not text.strip():
            raise ValueError('document_has_no_text')
        return
    files, xml, entries = _container(content)
    if input_format == 'docx':
        types, rels, document = (xml.get(name) for name in ('[Content_Types].xml', '_rels/.rels', 'word/document.xml'))
        if (types is None or types.tag != '{' + _CT + '}Types'
                or rels is None or rels.tag != '{' + _REL + '}Relationships'
                or document is None or document.tag != '{' + _W + '}document'):
            raise ValueError('invalid_docx_format')
        main_types = [item.get('ContentType') for item in types if item.tag == '{' + _CT + '}Override'
                      and item.get('PartName') == '/word/document.xml']
        main_rels = [item for item in rels if item.get('Type') == _OFFICE_REL]
        if (main_types != [_DOCX] or len(main_rels) != 1
                or main_rels[0].tag != '{' + _REL + '}Relationship'
                or main_rels[0].get('Target') != 'word/document.xml'
                or main_rels[0].get('TargetMode', 'Internal') != 'Internal'
                or len(document.findall('{' + _W + '}body')) != 1
                or any('macroEnabled' in item.get('ContentType', '') for item in types)):
            raise ValueError('invalid_docx_format')
        if not any((item.text or '').strip() for item in document.iter('{' + _W + '}t')):
            raise ValueError('document_has_no_text')
    else:
        manifest, document = xml.get('META-INF/manifest.xml'), xml.get('content.xml')
        if (files.get('mimetype') != _ODT.encode() or entries[0].filename != 'mimetype'
                or entries[0].compress_type != zipfile.ZIP_STORED
                or manifest is None or manifest.tag != '{' + _MANIFEST + '}manifest'
                or document is None or document.tag != '{' + _OFFICE + '}document-content'):
            raise ValueError('invalid_odt_format')
        roots = [item.get('{' + _MANIFEST + '}media-type') for item in manifest
                 if item.tag == '{' + _MANIFEST + '}file-entry'
                 and item.get('{' + _MANIFEST + '}full-path') == '/']
        contents = [item.get('{' + _MANIFEST + '}media-type') for item in manifest
                    if item.tag == '{' + _MANIFEST + '}file-entry'
                    and item.get('{' + _MANIFEST + '}full-path') == 'content.xml']
        body = document.find('{' + _OFFICE + '}body/{' + _OFFICE + '}text')
        if (roots != [_ODT] or contents != ['text/xml'] or body is None
                or any(True for _ in manifest.iter('{' + _MANIFEST + '}encryption-data'))):
            raise ValueError('invalid_odt_format')
        if not ''.join(body.itertext()).strip():
            raise ValueError('document_has_no_text')


def fixture(input_format, text):
    """Core-owned synthetic gate input. It contains no task or owner data."""
    from xml.sax.saxutils import escape
    if input_format == 'txt':
        return text.encode('utf-8')
    safe = escape(text)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        def write(name, value, compression=zipfile.ZIP_DEFLATED):
            entry = zipfile.ZipInfo(name, (2000, 1, 1, 0, 0, 0))
            entry.compress_type = compression
            archive.writestr(entry, value)
        if input_format == 'docx':
            write('[Content_Types].xml', f'<Types xmlns="{_CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="{_DOCX}"/></Types>')
            write('_rels/.rels', f'<Relationships xmlns="{_REL}"><Relationship Id="rId1" Type="{_OFFICE_REL}" Target="word/document.xml"/></Relationships>')
            write('word/document.xml', f'<w:document xmlns:w="{_W}"><w:body><w:p><w:r><w:t xml:space="preserve">{safe}</w:t></w:r></w:p></w:body></w:document>')
        elif input_format == 'odt':
            write('mimetype', _ODT, zipfile.ZIP_STORED)
            write('META-INF/manifest.xml', f'<manifest:manifest xmlns:manifest="{_MANIFEST}" manifest:version="1.2"><manifest:file-entry manifest:full-path="/" manifest:media-type="{_ODT}"/><manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>')
            write('content.xml', f'<office:document-content xmlns:office="{_OFFICE}" xmlns:text="{_TEXT}" office:version="1.2"><office:body><office:text><text:p>{safe}</text:p></office:text></office:body></office:document-content>')
        else:
            raise ValueError('unsupported_document_format')
    return output.getvalue()


def gate_cases(input_format):
    profile(input_format)
    return tuple((fixture(input_format, text), text) for text in (
        'SOLVIO bound document.', 'Grüße, Fahrrad & Café. Zweite Prüfung.'))

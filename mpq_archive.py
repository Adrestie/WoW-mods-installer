# -*- coding: utf-8 -*-
"""Reads and writes MPQ archives in pure Python (MIT licence).

Why not StormLib: it treats a v1 archive whose tables lie beyond 2 GB (bit
0x80000000 of their offset) as malformed, hence read-only. The 3.3.5 client
reads those offsets as unsigned 32-bit values; this module does the same.

Reading: formats v1 and v2 (hi-block table included), sector-based or
single-unit files, with or without sector checksums, zlib or bzip2
compression, encrypted or not.

Writing into an existing archive: new data and both tables are appended at
the end of the file and the header is rewritten last, so an interruption
before that final write leaves the previous archive intact. A replaced file
keeps its block entry; a new file takes a free hash entry and a new block,
and its name is added to (listfile). An existing (attributes) is kept up to
date. The space of old file versions is not reclaimed there.

Compaction (compact_archive) gives it back: the archive is written again
beside itself with only the files it holds, each with its stored bytes, then
compared with the old one and put in its place.

Refused before any byte is written: an unknown compression (PKWARE implode,
LZMA, audio), a signed archive (strong "NGIS" signature), a full hash table,
a v1 archive that would exceed 4 GB.
"""
import bz2
import hashlib
import os
import struct
import time
import zlib

M32 = 0xFFFFFFFF
MPQ_SIGNATURE = b"MPQ\x1a"
USER_DATA_SIGNATURE = b"MPQ\x1b"

# Block flags.
FILE_IMPLODE = 0x00000100
FILE_COMPRESS = 0x00000200
FILE_ENCRYPTED = 0x00010000
FILE_FIX_KEY = 0x00020000
FILE_SINGLE_UNIT = 0x01000000
FILE_DELETE_MARKER = 0x02000000
FILE_SECTOR_CRC = 0x04000000
FILE_EXISTS = 0x80000000

# Special block indices of a hash entry.
HASH_ENTRY_EMPTY = 0xFFFFFFFF
HASH_ENTRY_DELETED = 0xFFFFFFFE

# (attributes) flags.
ATTR_CRC32, ATTR_FILETIME, ATTR_MD5, ATTR_PATCH_BIT = 0x1, 0x2, 0x4, 0x8


class MpqError(Exception):
    pass


def _crypt_table():
    table = [0] * 0x500
    seed = 0x00100001
    for i in range(0x100):
        index = i
        for _ in range(5):
            seed = (seed * 125 + 3) % 0x2AAAAB
            high = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB
            table[index] = high | (seed & 0xFFFF)
            index += 0x100
    return table


CRYPT_TABLE = _crypt_table()


def hash_string(name, hash_type):
    """Hash of a file name. hash_type: 0 = table offset, 1 and 2 = check values, 3 = file key."""
    s1, s2 = 0x7FED7FED, 0xEEEEEEEE
    for c in name.replace("/", "\\").encode("latin-1"):
        if 0x61 <= c <= 0x7A:
            c -= 0x20
        s1 = (CRYPT_TABLE[(hash_type << 8) + c] ^ (s1 + s2)) & M32
        s2 = (c + s1 + s2 + (s2 << 5) + 3) & M32
    return s1


def decrypt(data, key):
    n = len(data) // 4
    words = list(struct.unpack_from("<%dI" % n, data))
    s = 0xEEEEEEEE
    for i in range(n):
        s = (s + CRYPT_TABLE[0x400 + (key & 0xFF)]) & M32
        plain = words[i] ^ ((key + s) & M32)
        key = ((((~key) << 0x15) + 0x11111111) | (key >> 0x0B)) & M32
        s = (plain + s + (s << 5) + 3) & M32
        words[i] = plain
    return struct.pack("<%dI" % n, *words) + bytes(data[n * 4:])


def encrypt(data, key):
    n = len(data) // 4
    words = list(struct.unpack_from("<%dI" % n, data))
    s = 0xEEEEEEEE
    for i in range(n):
        s = (s + CRYPT_TABLE[0x400 + (key & 0xFF)]) & M32
        plain = words[i]
        words[i] = plain ^ ((key + s) & M32)
        key = ((((~key) << 0x15) + 0x11111111) | (key >> 0x0B)) & M32
        s = (plain + s + (s << 5) + 3) & M32
    return struct.pack("<%dI" % n, *words) + bytes(data[n * 4:])


HASH_TABLE_KEY = hash_string("(hash table)", 3)
BLOCK_TABLE_KEY = hash_string("(block table)", 3)


def _decompress(chunk, expected, name):
    """A compressed sector (or single-unit file); its first byte is the method."""
    method, rest = chunk[0], chunk[1:]
    if method == 0x02:
        raw = zlib.decompress(rest)
    elif method == 0x10:
        raw = bz2.decompress(rest)
    else:
        raise MpqError("%s: compression 0x%02X is not supported (only zlib and bzip2 are)" % (name, method))
    if len(raw) != expected:
        raise MpqError("%s: %d bytes decompressed, %d expected" % (name, len(raw), expected))
    return raw


def _compress_sectors(raw, sector_size):
    """The file as zlib sectors, preceded by their (unencrypted) offset table."""
    count = max(1, (len(raw) + sector_size - 1) // sector_size)
    chunks = []
    for i in range(count):
        sector = raw[i * sector_size:(i + 1) * sector_size]
        packed = zlib.compress(sector, 9)
        # A sector that does not shrink is stored as is, without a method byte.
        chunks.append(b"\x02" + packed if len(packed) + 1 < len(sector) else sector)
    offsets, pos = [], (count + 1) * 4
    for c in chunks:
        offsets.append(pos)
        pos += len(c)
    offsets.append(pos)
    return struct.pack("<%dI" % (count + 1), *offsets) + b"".join(chunks)


class Archive(object):
    """An MPQ archive opened for reading; its tables stay in memory."""

    def __init__(self, path):
        self.path = path
        self.file_size = os.path.getsize(path)
        with open(path, "rb") as f:
            self.offset = self._find_header(f)
            f.seek(self.offset)
            header = f.read(44)
            (_, _, self.archive_size, self.version, sector_shift,
             hash_pos, block_pos, self.hash_count, self.block_count) = struct.unpack_from("<4sIIHHIIII", header)
            if self.version > 1:
                raise MpqError("%s: MPQ format v%d is not supported (v1 and v2 only)" % (path, self.version + 1))
            self.sector_size = 512 << (sector_shift & 0xFF)
            self.hi_block_table_pos = 0
            if self.version == 1:
                self.hi_block_table_pos, hash_high, block_high = struct.unpack_from("<QHH", header, 32)
                hash_pos |= hash_high << 32
                block_pos |= block_high << 32
            if self.hash_count == 0 or self.hash_count & (self.hash_count - 1):
                raise MpqError("%s: hash table of %d entries, not a power of two" % (path, self.hash_count))
            f.seek(self.offset + hash_pos)
            raw = decrypt(f.read(self.hash_count * 16), HASH_TABLE_KEY)
            self.hash_table = [list(struct.unpack_from("<IIHHI", raw, i * 16)) for i in range(self.hash_count)]
            f.seek(self.offset + block_pos)
            raw = decrypt(f.read(self.block_count * 16), BLOCK_TABLE_KEY)
            self.block_table = [list(struct.unpack_from("<IIII", raw, i * 16)) for i in range(self.block_count)]
            self.hi_blocks = [0] * self.block_count
            if self.hi_block_table_pos:
                f.seek(self.offset + self.hi_block_table_pos)
                self.hi_blocks = list(struct.unpack("<%dH" % self.block_count, f.read(self.block_count * 2)))
            # A strong signature follows the archive: changing the archive would break it.
            end = self.offset + self.archive_size
            f.seek(end)
            self.signed = self.file_size > end and f.read(4) == b"NGIS"

    @staticmethod
    def _find_header(f):
        start = f.read(16)
        if start[:4] == MPQ_SIGNATURE:
            return 0
        if start[:4] == USER_DATA_SIGNATURE:
            return struct.unpack_from("<I", start, 8)[0]
        raise MpqError("not an MPQ archive (no signature at the start of the file)")

    def _hash_index(self, name):
        """Index of the file's hash entry, or None."""
        mask = self.hash_count - 1
        start = hash_string(name, 0) & mask
        a, b = hash_string(name, 1), hash_string(name, 2)
        for step in range(self.hash_count):
            i = (start + step) & mask
            e = self.hash_table[i]
            if e[4] == HASH_ENTRY_EMPTY:
                return None
            if e[0] == a and e[1] == b and e[4] != HASH_ENTRY_DELETED and e[4] < self.block_count:
                block = self.block_table[e[4]]
                if block[3] & FILE_EXISTS and not block[3] & FILE_DELETE_MARKER:
                    return i
        return None

    def contains(self, name):
        return self._hash_index(name) is not None

    def size(self, name):
        """Size of a file once read (uncompressed), in bytes."""
        i = self._hash_index(name)
        if i is None:
            raise MpqError("%s is not in %s" % (name, self.path))
        return self.block_table[self.hash_table[i][4]][2]

    def read(self, name):
        i = self._hash_index(name)
        if i is None:
            raise MpqError("%s is not in %s" % (name, self.path))
        return self.read_block(self.hash_table[i][4], name)

    def read_block(self, index, name):
        """The content of the file of this block entry (name: for its key, when it is encrypted)."""
        position, packed_size, size, flags = self.block_table[index]
        position |= self.hi_blocks[index] << 32
        if flags & FILE_IMPLODE:
            raise MpqError("%s: PKWARE compression (implode) is not supported" % name)
        with open(self.path, "rb") as f:
            f.seek(self.offset + position)
            data = f.read(packed_size)
        key = 0
        if flags & FILE_ENCRYPTED:
            key = hash_string(name.replace("/", "\\").split("\\")[-1], 3)
            if flags & FILE_FIX_KEY:
                key = ((key + (position & M32)) ^ size) & M32
        compressed = bool(flags & FILE_COMPRESS)
        if flags & FILE_SINGLE_UNIT:
            if flags & FILE_ENCRYPTED:
                data = decrypt(data, key)
            if compressed and packed_size < size:
                return _decompress(data, size, name)
            return data[:size]
        count = (size + self.sector_size - 1) // self.sector_size
        if not compressed:
            chunks = []
            for s in range(count):
                sector = data[s * self.sector_size:(s + 1) * self.sector_size]
                chunks.append(decrypt(sector, (key + s) & M32) if flags & FILE_ENCRYPTED else sector)
            return b"".join(chunks)[:size]
        offset_count = count + 1 + (1 if flags & FILE_SECTOR_CRC else 0)
        table = data[:offset_count * 4]
        if flags & FILE_ENCRYPTED:
            table = decrypt(table, (key - 1) & M32)
        offsets = struct.unpack("<%dI" % offset_count, table)
        chunks = []
        for s in range(count):
            expected = min(self.sector_size, size - s * self.sector_size)
            sector = data[offsets[s]:offsets[s + 1]]
            if flags & FILE_ENCRYPTED:
                sector = decrypt(sector, (key + s) & M32)
            chunks.append(_decompress(sector, expected, name) if len(sector) < expected else sector[:expected])
        return b"".join(chunks)


def _listfile_names(text):
    return [n for n in text.replace("\r", "\n").replace(";", "\n").split("\n") if n.strip()]


def _filetime_now():
    return int((time.time() + 11644473600) * 10000000)


def _parse_attributes(raw, blocks):
    """(version, flags, entries, patch-bit shape, crcs, times, md5s, tail) of an (attributes) file
    written for a block table of this many blocks, or None for an unknown layout.

    The layouts recognised are those StormLib accepts: as many entries as blocks or one fewer, and for
    the patch bit a bit array, a word array or nothing. crcs, times, md5s: lists, None when absent;
    tail: the bytes after them (the patch bits)."""
    if len(raw) < 8:
        return None
    version, flags = struct.unpack_from("<II", raw)
    if version != 100 or flags & ~0xF:
        return None
    per_entry = (4 if flags & ATTR_CRC32 else 0) + (8 if flags & ATTR_FILETIME else 0) +                 (16 if flags & ATTR_MD5 else 0)
    patch_bit = bool(flags & ATTR_PATCH_BIT)
    layouts = []                                 # (entries, shape of the patch-bit array)
    layouts.append((blocks, "bits" if patch_bit else "none"))
    layouts.append((blocks - 1, "bits" if patch_bit else "none"))
    if patch_bit:
        layouts.append((blocks, "none"))
        layouts.append((blocks, "words"))
    for n, shape in layouts:
        if n > 0 and len(raw) == 8 + n * per_entry + PATCH_SIZE[shape](blocks):
            break
    else:
        return None
    pos = 8
    crcs = times = md5s = None
    if flags & ATTR_CRC32:
        crcs = list(struct.unpack_from("<%dI" % n, raw, pos))
        pos += 4 * n
    if flags & ATTR_FILETIME:
        times = list(struct.unpack_from("<%dQ" % n, raw, pos))
        pos += 8 * n
    if flags & ATTR_MD5:
        md5s = [raw[pos + 16 * k:pos + 16 * (k + 1)] for k in range(n)]
        pos += 16 * n
    return version, flags, n, shape, crcs, times, md5s, raw[pos:]


PATCH_SIZE = {"none": lambda n: 0, "bits": lambda n: (n + 6) // 8, "words": lambda n: n * 4}


def _attributes_bytes(version, flags, crcs, times, md5s, patch):
    out = struct.pack("<II", version, flags)
    if crcs is not None:
        out += struct.pack("<%dI" % len(crcs), *crcs)
    if times is not None:
        out += struct.pack("<%dQ" % len(times), *times)
    if md5s is not None:
        out += b"".join(md5s)
    return out + patch


def _updated_attributes(raw, blocks_before, blocks_after, written):
    """(attributes) resized for the new block table, with the checksums of the
    written files.

    raw: current (attributes) content; blocks_before / blocks_after: block
    table sizes; written: {block index: content}. Returns None for an
    unknown layout (_parse_attributes), and the file is then left as it is."""
    parsed = _parse_attributes(raw, blocks_before)
    if parsed is None:
        return None
    version, flags, n, shape, crcs, times, md5s, tail = parsed
    gap = blocks_before - n                      # 0 or 1: the same layout is kept
    n2 = blocks_after - gap
    if crcs is not None:
        crcs += [0] * (n2 - n)
    if times is not None:
        times += [0] * (n2 - n)
    if md5s is not None:
        md5s += [b"\0" * 16] * (n2 - n)
    now = _filetime_now()
    for index, content in written.items():
        if index < n2:
            if crcs is not None:
                crcs[index] = zlib.crc32(content) & M32
            if times is not None:
                times[index] = now
            if md5s is not None:
                md5s[index] = hashlib.md5(content).digest()
    new_size = PATCH_SIZE[shape](blocks_after)
    return _attributes_bytes(version, flags, crcs, times, md5s,
                             tail[:new_size] + b"\0" * max(0, new_size - len(tail)))


def _remapped_attributes(raw, blocks_before, order):
    """(attributes) for a block table rebuilt from these old block indices, in their new order, each
    block keeping its checksums; None for an unknown layout."""
    parsed = _parse_attributes(raw, blocks_before)
    if parsed is None:
        return None
    version, flags, n, shape, crcs, times, md5s, tail = parsed
    n2 = len(order) - (blocks_before - n)
    pick = [order[k] for k in range(n2)]

    def remap(values, empty):
        return None if values is None else [values[i] if i < n else empty for i in pick]

    patch = b""
    if shape == "bits":
        bits = bytearray(PATCH_SIZE["bits"](len(order)))
        for k, i in enumerate(order):
            if i // 8 < len(tail) and tail[i // 8] & (0x80 >> (i % 8)):
                bits[k // 8] |= 0x80 >> (k % 8)
        patch = bytes(bits)
    elif shape == "words":
        words = struct.unpack_from("<%dI" % blocks_before, tail)
        patch = struct.pack("<%dI" % len(order), *[words[i] for i in order])
    return _attributes_bytes(version, flags, remap(crcs, 0), remap(times, 0), remap(md5s, b"\0" * 16), patch)


def has_room(path, sizes):
    """True if the archive can take these files without its hash table filling up or, for a v1
    archive, going past 4 GB. sizes: {name: size in bytes}; the size stored is bounded from it."""
    a = Archive(path)
    new = [n for n in sizes if not a.contains(n)]
    free = sum(1 for e in a.hash_table if e[4] in (HASH_ENTRY_EMPTY, HASH_ENTRY_DELETED))
    # One entry always stays free (see write_into_archive).
    if free - len(new) < 1:
        return False
    if a.version != 0:
        return True
    # Sector offsets, method bytes, the rewritten (listfile) and (attributes), the tables.
    stored = sum(n + (n // a.sector_size + 2) * 5 for n in sizes.values())
    for special, extra in (("(listfile)", sum(len(n) + 2 for n in new)), ("(attributes)", 28 * len(new))):
        if a.contains(special):
            stored += a.size(special) + extra + 64
    end = max(a.file_size, a.offset + a.archive_size) - a.offset + stored +         (a.hash_count + a.block_count + len(new)) * 16
    return end <= M32


def write_into_archive(path, files, remove=(), check_only=False):
    """Writes files into the existing archive and removes others from it.

    files: {name: content}. remove: names to delete; their hash entry is
    marked deleted (a lookup goes on past it), their block entry cleared and
    their name dropped from (listfile). Everything is prepared and checked in
    memory before the first write; check_only: stop there, writing nothing
    (an MpqError says the write would be refused)."""
    a = Archive(path)
    if a.signed:
        raise MpqError("%s carries a strong signature: changing it would invalidate it" % path)
    mask = a.hash_count - 1
    blocks = [list(b) for b in a.block_table]
    hi_blocks = list(a.hi_blocks)
    hash_table = [list(e) for e in a.hash_table]
    contents = {}                                # block index -> content to write
    new_names = []

    removed = []
    for name in remove:
        i = a._hash_index(name)
        if i is None or name in files:
            continue
        index = hash_table[i][4]
        hash_table[i] = [M32, M32, 0xFFFF, 0xFFFF, HASH_ENTRY_DELETED]
        blocks[index] = [0, 0, 0, 0]
        hi_blocks[index] = 0
        removed.append(name)

    # An existing file keeps its block; a new one takes a free hash entry and a new block.
    for name, content in files.items():
        i = a._hash_index(name)
        if i is not None:
            contents[hash_table[i][4]] = content
            continue
        free = [j for j in range(a.hash_count) if hash_table[j][4] in (HASH_ENTRY_EMPTY, HASH_ENTRY_DELETED)]
        # A table without a free entry can no longer answer "absent": one always stays free.
        if len(free) < 2:
            raise MpqError("%s: hash table full, cannot add %s" % (path, name))
        start = hash_string(name, 0) & mask
        for step in range(a.hash_count):
            j = (start + step) & mask
            if hash_table[j][4] in (HASH_ENTRY_EMPTY, HASH_ENTRY_DELETED):
                break
        hash_table[j] = [hash_string(name, 1), hash_string(name, 2), 0, 0, len(blocks)]
        contents[len(blocks)] = content
        blocks.append([0, 0, 0, 0])
        hi_blocks.append(0)
        new_names.append(name)

    if (new_names or removed) and a.contains("(listfile)"):
        before = a.read("(listfile)").decode("latin-1")
        text = before
        if removed:
            gone = set(n.upper() for n in removed)
            text = "".join(line for line in before.splitlines(True) if line.strip().upper() not in gone)
        known = set(n.upper() for n in _listfile_names(text))
        added = [n for n in new_names if n.upper() not in known]
        if added:
            text = text.rstrip("\r\n") + "\r\n" + "\r\n".join(added) + "\r\n"
        if text != before:
            contents[hash_table[a._hash_index("(listfile)")][4]] = text.encode("latin-1")
    if a.contains("(attributes)"):
        attr_index = hash_table[a._hash_index("(attributes)")][4]
        attrs = _updated_attributes(a.read("(attributes)"), len(a.block_table), len(blocks),
                                    {k: v for k, v in contents.items() if k != attr_index})
        if attrs is not None:
            contents[attr_index] = attrs

    stored = [(index, content, _compress_sectors(content, a.sector_size)) for index, content in contents.items()]
    start = max(a.file_size, a.offset + a.archive_size)
    pos = start - a.offset
    for index, content, packed in stored:
        blocks[index] = [pos & M32, len(packed), len(content), FILE_EXISTS | FILE_COMPRESS]
        hi_blocks[index] = pos >> 32
        pos += len(packed)
    hash_pos = pos
    block_pos = hash_pos + len(hash_table) * 16
    end = block_pos + len(blocks) * 16
    hi_pos = 0
    if a.version == 1 and (a.hi_block_table_pos or any(hi_blocks)):
        hi_pos = end
        end += len(hi_blocks) * 2
    if a.version == 0 and end > M32:
        raise MpqError("%s: a v1 archive cannot exceed 4 GB" % path)
    if check_only:
        return len(files)

    # Data then tables after everything the file holds; the header last, so
    # that until then the archive described is still the old one.
    with open(path, "r+b") as f:
        f.seek(start)
        for _, _, packed in stored:
            f.write(packed)
        f.write(encrypt(b"".join(struct.pack("<IIHHI", *e) for e in hash_table), HASH_TABLE_KEY))
        f.write(encrypt(b"".join(struct.pack("<IIII", *b) for b in blocks), BLOCK_TABLE_KEY))
        if hi_pos:
            f.write(struct.pack("<%dH" % len(hi_blocks), *hi_blocks))
        f.flush()
        os.fsync(f.fileno())
        f.seek(a.offset)
        header = bytearray(f.read(44 if a.version == 1 else 32))
        struct.pack_into("<I", header, 8, end & M32)
        struct.pack_into("<II", header, 16, hash_pos & M32, block_pos & M32)
        struct.pack_into("<II", header, 24, len(hash_table), len(blocks))
        if a.version == 1:
            struct.pack_into("<QHH", header, 32, hi_pos, hash_pos >> 32, block_pos >> 32)
        f.seek(a.offset)
        f.write(bytes(header))
        f.flush()
        os.fsync(f.fileno())
    return len(files)


def create_archive(path, files, hash_entries=1024):
    """Creates a v1 archive holding files ({name: content}) and a (listfile).

    hash_entries: size of the hash table, a power of two."""
    files = dict(files)
    files["(listfile)"] = ("\r\n".join(n for n in files if n != "(listfile)") + "\r\n").encode("latin-1")
    sector_size = 4096
    hash_table = [[M32, M32, 0xFFFF, 0xFFFF, HASH_ENTRY_EMPTY] for _ in range(hash_entries)]
    blocks, chunks, pos = [], [], 32
    for name, content in files.items():
        packed = _compress_sectors(content, sector_size)
        i = hash_string(name, 0) & (hash_entries - 1)
        while hash_table[i][4] != HASH_ENTRY_EMPTY:
            i = (i + 1) & (hash_entries - 1)
        hash_table[i] = [hash_string(name, 1), hash_string(name, 2), 0, 0, len(blocks)]
        blocks.append((pos, len(packed), len(content), FILE_EXISTS | FILE_COMPRESS))
        chunks.append(packed)
        pos += len(packed)
    hash_pos = pos
    block_pos = hash_pos + hash_entries * 16
    end = block_pos + len(blocks) * 16
    with open(path, "wb") as f:
        f.write(struct.pack("<4sIIHHIIII", MPQ_SIGNATURE, 32, end, 0, 3, hash_pos, block_pos, hash_entries, len(blocks)))
        f.write(b"".join(chunks))
        f.write(encrypt(b"".join(struct.pack("<IIHHI", *e) for e in hash_table), HASH_TABLE_KEY))
        f.write(encrypt(b"".join(struct.pack("<IIII", *b) for b in blocks), BLOCK_TABLE_KEY))


# ------------------------------------------------------------------ compaction

def _file_key(name, position, size, flags):
    """The key of an encrypted file stored at this position."""
    key = hash_string(name.replace("/", "\\").split("\\")[-1], 3)
    if flags & FILE_FIX_KEY:
        key = ((key + (position & M32)) ^ size) & M32
    return key


def _recrypt(data, flags, size, sector_size, old_key, new_key):
    """A stored encrypted file, encrypted again with another key; its bytes are otherwise the same."""
    if flags & FILE_SINGLE_UNIT:
        return encrypt(decrypt(data, old_key), new_key)
    count = (size + sector_size - 1) // sector_size
    out = bytearray(data)
    if flags & (FILE_COMPRESS | FILE_IMPLODE):
        n = count + 1 + (1 if flags & FILE_SECTOR_CRC else 0)
        table = decrypt(data[:n * 4], (old_key - 1) & M32)
        out[:n * 4] = encrypt(table, (new_key - 1) & M32)
        offsets = struct.unpack("<%dI" % n, table)
        bounds = [(offsets[s], offsets[s + 1]) for s in range(count)]
    else:
        bounds = [(s * sector_size, min(len(data), (s + 1) * sector_size)) for s in range(count)]
    for s, (start, end) in enumerate(bounds):
        out[start:end] = encrypt(decrypt(data[start:end], (old_key + s) & M32), (new_key + s) & M32)
    return bytes(out)


def _live_entries(a):
    """[hash index] of the entries that lead to a file (a deletion marker of a patch archive included)."""
    return [i for i, e in enumerate(a.hash_table)
            if e[4] < a.block_count and a.block_table[e[4]][3] & FILE_EXISTS]


def _known_names(a, names):
    """{(hash 1, hash 2): name} of the names of (listfile), of the special files and of names."""
    listed = _listfile_names(a.read("(listfile)").decode("latin-1")) if a.contains("(listfile)") else []
    found = {}
    for n in list(names) + listed + ["(listfile)", "(attributes)", "(signature)"]:
        n = n.strip().replace("/", "\\")
        found.setdefault((hash_string(n, 1), hash_string(n, 2)), n)
    return found


def _compaction(a, names=(), room=0):
    """The compacted archive, worked out in memory: {"order": old block indices in their new order (that
    of their place in the file), "blocks", "hi_blocks", "positions" (of the blocks of order), "hash_table",
    "stored" {old block index: (bytes stored anew, size)}, "name_of_block" {old block index: name}, "hash_pos",
    "block_pos", "hi_pos", "end"}, places relative to the start of the archive.

    The hash table is rebuilt without its deleted entries when every file has a known name ((listfile),
    the special files, names), twice as large while it would be more than three quarters full (counting
    room entries more, for the files about to be written); otherwise
    it keeps its layout, the entries of the files gone marked deleted."""
    if a.signed:
        raise MpqError("%s carries a strong signature: changing it would invalidate it" % a.path)
    live = _live_entries(a)
    known = _known_names(a, names)
    named = {i: known.get((a.hash_table[i][0], a.hash_table[i][1])) for i in live}
    order = sorted({a.hash_table[i][4] for i in live},
                   key=lambda b: ((a.hi_blocks[b] << 32) | a.block_table[b][0], b))
    new_index = {b: k for k, b in enumerate(order)}
    name_of_block = {a.hash_table[i][4]: named[i] for i in live if named[i]}
    for b in order:
        if a.block_table[b][3] & FILE_ENCRYPTED and a.block_table[b][3] & FILE_FIX_KEY and b not in name_of_block:
            raise MpqError("%s: a file whose key depends on its place has no known name, it cannot be moved"
                           % a.path)

    stored = {}
    attributes = next((b for b, n in name_of_block.items() if n == "(attributes)"), None)
    if attributes is not None:
        content = _remapped_attributes(a.read_block(attributes, "(attributes)"), a.block_count, order)
        if content is None:
            raise MpqError("%s: its (attributes) has a layout the installer does not know" % a.path)
        stored[attributes] = (_compress_sectors(content, a.sector_size), len(content))

    if all(named[i] for i in live):
        size = a.hash_count
        while (len(live) + 1 + room) * 4 > size * 3:
            size *= 2
        hash_table = [[M32, M32, 0xFFFF, 0xFFFF, HASH_ENTRY_EMPTY] for _ in range(size)]
        for i in live:
            e = a.hash_table[i]
            j = hash_string(named[i], 0) & (size - 1)
            while hash_table[j][4] != HASH_ENTRY_EMPTY:
                j = (j + 1) & (size - 1)
            hash_table[j] = [e[0], e[1], e[2], e[3], new_index[e[4]]]
    else:
        hash_table = []
        for i, e in enumerate(a.hash_table):
            if i in named:
                hash_table.append([e[0], e[1], e[2], e[3], new_index[e[4]]])
            elif e[4] == HASH_ENTRY_EMPTY:
                hash_table.append(list(e))
            else:
                hash_table.append([M32, M32, 0xFFFF, 0xFFFF, HASH_ENTRY_DELETED])

    with open(a.path, "rb") as f:
        f.seek(a.offset + 4)
        pos = struct.unpack("<I", f.read(4))[0]          # the size of the header: the files follow it
    positions, blocks = [], []
    for b in order:
        position, packed, size, flags = a.block_table[b]
        if b in stored:
            packed, size, flags = len(stored[b][0]), stored[b][1], FILE_EXISTS | FILE_COMPRESS
        positions.append(pos)
        blocks.append([pos & M32, packed, size, flags])
        pos += packed
    hi_blocks = [p >> 32 for p in positions]
    hash_pos = pos
    block_pos = hash_pos + len(hash_table) * 16
    end = block_pos + len(blocks) * 16
    hi_pos = 0
    if a.version == 1 and any(hi_blocks):
        hi_pos = end
        end += len(hi_blocks) * 2
    if a.version == 0 and end > M32:
        raise MpqError("%s: a v1 archive cannot exceed 4 GB" % a.path)
    return {"order": order, "blocks": blocks, "hi_blocks": hi_blocks, "positions": positions,
            "hash_table": hash_table, "stored": stored, "name_of_block": name_of_block,
            "hash_pos": hash_pos, "block_pos": block_pos, "hi_pos": hi_pos, "end": end}


def compacted_size(path, names=()):
    """(size of the file now, its size once compacted, bytes of the files it keeps); writes nothing."""
    a = Archive(path)
    c = _compaction(a, names)
    return a.file_size, a.offset + c["end"], sum(b[1] for b in c["blocks"])


def _same_files(a, b, name_of_block):
    """Raises MpqError unless archive b leads every entry of archive a (name, language, platform) to the
    same file: same sizes and flags, same stored bytes, or the same content once read for a file
    encrypted again; and finds each known name the same way. (attributes), rewritten, only has to read."""
    def entries(x):
        return {tuple(x.hash_table[i][:4]): x.hash_table[i][4] for i in _live_entries(x)}
    old, new = entries(a), entries(b)
    if set(old) != set(new):
        raise MpqError("%s: the compacted copy does not hold the same files" % a.path)
    with open(a.path, "rb") as fa, open(b.path, "rb") as fb:
        for key, i in old.items():
            j = new[key]
            name = name_of_block.get(i)
            if name == "(attributes)":
                b.read_block(j, name)                     # rewritten: it must still read
                continue
            ob, nb = a.block_table[i], b.block_table[j]
            if ob[1:] != nb[1:]:
                raise MpqError("%s: %s differs in the compacted copy" % (a.path, name or "a file"))
            if ob[3] & FILE_ENCRYPTED and ob[3] & FILE_FIX_KEY:
                same = a.read_block(i, name) == b.read_block(j, name)
            else:
                fa.seek(a.offset + ((a.hi_blocks[i] << 32) | ob[0]))
                fb.seek(b.offset + ((b.hi_blocks[j] << 32) | nb[0]))
                same = fa.read(ob[1]) == fb.read(nb[1])
            if not same:
                raise MpqError("%s: %s differs in the compacted copy" % (a.path, name or "a file"))
    for name in set(name_of_block.values()):
        if a.contains(name) != b.contains(name) or \
                (a.contains(name) and name != "(attributes)" and a.size(name) != b.size(name)):
            raise MpqError("%s: %s is not found the same way in the compacted copy" % (a.path, name))


def compact_archive(path, names=(), room=0):
    """Rewrites the archive with the files it holds and nothing else: the space left by the files removed
    or replaced, and by the tables of earlier writes, is given back. Every file keeps its name, language
    and stored bytes (one whose key depends on its place is encrypted again, same content); (listfile)
    is unchanged, (attributes) follows the new block numbers. The new archive is written beside the old
    one (<name>.compact-tmp), compared with it, then put in its place: until then the old one is intact.
    names: file names known besides those of (listfile); room: hash table entries to leave free for
    files about to be written (the table grows to take them). Returns (size before, size after)."""
    a = Archive(path)
    c = _compaction(a, names, room)
    temporary = path + ".compact-tmp"
    try:
        with open(path, "rb") as src, open(temporary, "wb") as out:
            out.write(src.read(a.offset))                 # what precedes the archive (user data)
            header = bytearray(src.read(c["positions"][0] if c["positions"] else c["hash_pos"]))
            struct.pack_into("<I", header, 8, c["end"] & M32)
            struct.pack_into("<II", header, 16, c["hash_pos"] & M32, c["block_pos"] & M32)
            struct.pack_into("<II", header, 24, len(c["hash_table"]), len(c["blocks"]))
            if a.version == 1:
                struct.pack_into("<QHH", header, 32, c["hi_pos"], c["hash_pos"] >> 32, c["block_pos"] >> 32)
            out.write(bytes(header))
            for k, b in enumerate(c["order"]):
                if b in c["stored"]:
                    out.write(c["stored"][b][0])
                    continue
                position, packed, size, flags = a.block_table[b]
                position |= a.hi_blocks[b] << 32
                src.seek(a.offset + position)
                data = src.read(packed)
                if flags & FILE_ENCRYPTED and flags & FILE_FIX_KEY:
                    name = c["name_of_block"][b]
                    data = _recrypt(data, flags, size, a.sector_size, _file_key(name, position, size, flags),
                                    _file_key(name, c["positions"][k], size, flags))
                out.write(data)
            out.write(encrypt(b"".join(struct.pack("<IIHHI", *e) for e in c["hash_table"]), HASH_TABLE_KEY))
            out.write(encrypt(b"".join(struct.pack("<IIII", *e) for e in c["blocks"]), BLOCK_TABLE_KEY))
            if c["hi_pos"]:
                out.write(struct.pack("<%dH" % len(c["hi_blocks"]), *c["hi_blocks"]))
            out.flush()
            os.fsync(out.fileno())
        _same_files(a, Archive(temporary), c["name_of_block"])
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return a.file_size, a.offset + c["end"]

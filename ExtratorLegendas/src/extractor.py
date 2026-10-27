"""Selective HTTP Range extraction of Matroska text subtitles. No video file is saved."""
from __future__ import annotations

import collections
import concurrent.futures
import copy
import dataclasses
import html
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
from urllib.parse import urlsplit
import zlib

import requests

MIB = 1024 * 1024
SUPPORTED = {'S_TEXT/UTF8', 'S_TEXT/ASS', 'S_TEXT/SSA', 'S_TEXT/WEBVTT'}


class ExtractionError(Exception):
    pass


class Cancelled(ExtractionError):
    pass


class TransferBudget:
    def __init__(self, limit):
        self.limit = limit
        self.transferred = 0
        self.requests = 0
        self.reserved = 0
        self.lock = threading.Lock()


class RangeSource:
    """Bounded, cached range reader. A 200 response is NEVER read as a fallback."""
    def __init__(self, url, limit_mib=128, cancel=None, notify=None):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ExtractionError('Cole um link HTTP/HTTPS do stream. Magnet e links de páginas não servem.')
        if parsed.username or parsed.password:
            raise ExtractionError('Links com usuário e senha na URL não são suportados.')
        self.url = url
        self._budget = TransferBudget(int(limit_mib * MIB))
        if not MIB <= self.limit <= 2048 * MIB:
            raise ExtractionError('O limite deve estar entre 1 e 2048 MiB.')
        self.cancel = cancel or threading.Event()
        self.notify = notify or (lambda: None)
        self.session = requests.Session()
        self.session.headers.update({'Accept-Encoding': 'identity', 'User-Agent': 'ExtratorLegendas/1.0'})
        self.size = None
        self.cache = collections.OrderedDict()
        self.validator = None
        self.waiting_since = None
        self.last_received = None
        self.peers = [self]
        try:
            data = self._fetch(0, 16)
            self.cache[0] = data
        except Exception:
            self.close()
            raise

    def check(self):
        if self.cancel.is_set():
            raise Cancelled('Operação cancelada. Nenhum arquivo incompleto foi salvo.')

    @property
    def transferred(self):
        return self._budget.transferred

    @property
    def requests(self):
        return self._budget.requests

    @property
    def limit(self):
        return self._budget.limit

    @limit.setter
    def limit(self, value):
        with self._budget.lock:
            self._budget.limit = value

    def fork(self):
        """Independent HTTP connection/cache; one shared, locked traffic budget."""
        peer = copy.copy(self)
        peer.session = requests.Session()
        peer.session.headers.update(self.session.headers)
        peer.cache = self.cache.copy()
        peer.waiting_since = None
        peer.last_received = None
        with self._budget.lock:
            self.peers.append(peer)
        return peer

    def _fetch(self, start, length):
        self.check()
        with self._budget.lock:
            if self.transferred + self._budget.reserved + length > self.limit:
                raise ExtractionError('Limite de transferência atingido. Nenhuma legenda incompleta foi salva. '
                                      'Tente o modo Econômico ou aumente o limite em MiB. '
                                      'O teto de 35% do vídeo continua valendo.')
            self._budget.reserved += length
        reservation = length
        headers = {'Range': f'bytes={start}-{start + length - 1}'}
        if self.validator:
            headers['If-Range'] = self.validator
        try:
            import time
            self.waiting_since = time.monotonic()
            with self.session.get(self.url, headers=headers, stream=True, timeout=(10, 20)) as response:
                with self._budget.lock:
                    self._budget.requests += 1
                if response.status_code != 206:
                    raise ExtractionError(
                        f'O servidor respondeu HTTP {response.status_code}, sem acesso parcial válido. '
                        'A leitura foi interrompida para não baixar o vídeo inteiro. '
                        'Mantenha o Stremio aberto e copie um link de stream atualizado.')
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                if not match:
                    raise ExtractionError('O servidor não informou um intervalo de bytes válido.')
                first, last, total = map(int, match.groups())
                if first != start or last != min(start + length, total) - 1 or total <= last:
                    raise ExtractionError('O servidor devolveu um intervalo diferente do solicitado.')
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise ExtractionError('O servidor comprimiu o stream HTTP; a leitura parcial não é segura.')
                if self.size is not None and self.size != total:
                    raise ExtractionError('O stream mudou de tamanho durante a extração. Copie o link novamente.')
                current = response.headers.get('ETag')
                if not current or current.startswith('W/'):
                    current = response.headers.get('Last-Modified')
                if self.validator and current and current != self.validator:
                    raise ExtractionError('O conteúdo do stream mudou durante a extração.')
                self.validator = current or self.validator
                self.size = total
                self.limit = min(self.limit, int(total * 0.35))
                needed = last - first + 1
                if self.transferred + needed > self.limit:
                    raise ExtractionError('A leitura ultrapassaria 35% do vídeo. Operação interrompida.')
                parts = []
                remaining = needed
                while remaining:
                    self.check()
                    chunk = response.raw.read(min(remaining, 8192), decode_content=False)
                    if not chunk:
                        raise ExtractionError('O servidor encerrou o stream antes de completar o trecho solicitado.')
                    with self._budget.lock:
                        self._budget.transferred += len(chunk)
                        self._budget.reserved -= len(chunk)
                    reservation -= len(chunk)
                    self.last_received = time.monotonic()
                    remaining -= len(chunk)
                    parts.append(chunk)
                    self.notify()
                return b''.join(parts)
        except requests.RequestException:
            raise ExtractionError('Falha de conexão com o stream. Verifique a internet e mantenha o Stremio aberto.') from None
        except (OSError, TimeoutError):
            raise ExtractionError('A conexão foi interrompida ou demorou demais. Tente um link atualizado.') from None
        finally:
            with self._budget.lock:
                self._budget.reserved -= reservation
            self.waiting_since = None

    def read(self, position, length, prefetch=0):
        self.check()
        if position < 0 or length < 0 or position + length > self.size:
            raise ExtractionError('Arquivo truncado ou estrutura de vídeo inválida.')
        if length > 8 * MIB:
            raise ExtractionError('Bloco de metadados ou legenda grande demais para esta versão.')
        if not length:
            return b''
        for start, data in list(self.cache.items())[::-1]:
            if start <= position and position + length <= start + len(data):
                self.cache.move_to_end(start)
                return data[position - start:position - start + length]
        amount = min(max(length, prefetch), self.size - position)
        data = self._fetch(position, amount)
        self.cache[position] = data
        while len(self.cache) > 32:
            self.cache.popitem(last=False)
        return data[:length]

    def close(self):
        for peer in self.peers:
            peer.session.close()


def vint(data, offset=0, keep_marker=False):
    if offset >= len(data) or not data[offset]:
        raise ExtractionError('Cabeçalho EBML inválido.')
    first = data[offset]
    width = 1
    marker = 128
    while not first & marker:
        marker >>= 1
        width += 1
    if offset + width > len(data):
        raise ExtractionError('Cabeçalho EBML incompleto.')
    value = int.from_bytes(data[offset:offset + width], 'big')
    if not keep_marker:
        value &= (1 << (7 * width)) - 1
    return value, width


@dataclasses.dataclass
class Element:
    id: int
    start: int
    data: int
    end: int
    unknown: bool = False


def element(source, position, bound, prefetch=0):
    head = source.read(position, min(12, bound - position), prefetch)
    eid, n = vint(head, keep_marker=True)
    if n > 4:
        raise ExtractionError('Identificador EBML inválido.')
    size, m = vint(head, n)
    unknown = size == (1 << (7 * m)) - 1
    begin = position + n + m
    end = bound if unknown else begin + size
    if end > bound or begin > bound:
        raise ExtractionError('O vídeo contém um elemento truncado.')
    return Element(eid, position, begin, end, unknown)


class MemorySource:
    def __init__(self, data):
        self.data = data
        self.size = len(data)

    def read(self, p, n, prefetch=0):
        if p < 0 or p + n > self.size:
            raise ExtractionError('Metadados truncados.')
        return self.data[p:p + n]


def children(data):
    src = MemorySource(data)
    p = 0
    while p < src.size:
        e = element(src, p, src.size)
        if e.unknown:
            raise ExtractionError('Tamanho indefinido em metadados não suportado.')
        yield e.id, data[e.data:e.end]
        p = e.end


def uint(data):
    return int.from_bytes(data, 'big')


def real(data):
    if len(data) not in (4, 8):
        raise ExtractionError('Número inválido nos metadados.')
    return struct.unpack('>f' if len(data) == 4 else '>d', data)[0]


def string(data):
    return data.decode('utf-8', errors='strict').rstrip('\0')


@dataclasses.dataclass
class Track:
    number: int
    codec: str
    language: str = 'und'
    name: str = ''
    private: bytes = b''
    default_ns: int = 0
    delay_ns: int = 0
    scale: float = 1.0
    forced: bool = False
    compression: list = dataclasses.field(default_factory=list)
    unsupported: str = ''
    uid: int = 0

    @property
    def usable(self):
        return self.codec in SUPPORTED and not self.unsupported


def parse_track(data):
    values = dict(children(data))
    if uint(values.get(0x83, b'')) != 17:
        return None
    t = Track(uint(values.get(0xD7, b'')), string(values.get(0x86, b'')))
    t.uid = uint(values.get(0x73C5, b''))
    t.language = string(values.get(0x22B59D, values.get(0x22B59C, b'und')))
    t.name = string(values.get(0x536E, b''))
    t.private = values.get(0x63A2, b'')
    t.default_ns = uint(values.get(0x23E383, b''))
    t.delay_ns = uint(values.get(0x56AA, b''))
    t.scale = real(values[0x23314F]) if 0x23314F in values else 1.0
    if not 0 < t.scale <= 1000000:
        t.unsupported = 'Escala de tempo inválida.'
    t.forced = bool(uint(values.get(0x55AA, b'')))
    if t.codec not in SUPPORTED:
        t.unsupported = 'Formato não suportado. Legendas em imagem (PGS/VobSub) precisam de OCR.'
    for eid, enc in children(values.get(0x6D80, b'')):
        if eid != 0x6240:
            continue
        fields = dict(children(enc))
        order = uint(fields.get(0x5031, b''))
        scope = uint(fields.get(0x5032, b'\x01'))
        kind = uint(fields.get(0x5033, b''))
        if kind or 0x5035 in fields or scope != 1:
            t.unsupported = 'Criptografia ou codificação de faixa não suportada.'
            continue
        comp = dict(children(fields.get(0x5034, b'')))
        algo = uint(comp.get(0x4254, b''))
        if algo not in (0, 3):
            t.unsupported = 'Compressão de legenda não suportada.'
        t.compression.append((order, algo, comp.get(0x4255, b'')))
    return t


@dataclasses.dataclass
class Cue:
    start: float
    end: float
    text: str
    order: int = 0


class Matroska:
    def __init__(self, source, prefetch=2048):
        self.source = source
        self.prefetch = prefetch
        self.scale = 1000000
        self.duration = None
        self.tracks = []
        self.segment = None
        self.first_cluster = None
        self.seek = {}
        self.method = 'Preparando extração'

    def inspect(self):
        s = self.source
        if s.read(0, min(4, s.size)) != b'\x1aE\xdf\xa3':
            raise ExtractionError('Este stream não é MKV/WebM. Esta versão não extrai de MP4, HLS ou páginas web.')
        p = 0
        while p < s.size:
            e = element(s, p, s.size)
            if e.id == 0x18538067:
                self.segment = e
                break
            if e.unknown:
                break
            p = e.end
        if not self.segment:
            raise ExtractionError('Segmento Matroska não encontrado.')
        found_info = False
        found_tracks = False
        p = self.segment.data
        while p < self.segment.end:
            e = element(s, p, self.segment.end)
            if e.id == 0x114D9B74:
                for eid, body in children(s.read(e.data, e.end - e.data)):
                    if eid == 0x4DBB:
                        fields = dict(children(body))
                        if 0x53AB in fields and 0x53AC in fields:
                            self.seek[uint(fields[0x53AB])] = self.segment.data + uint(fields[0x53AC])
            elif e.id == 0x1549A966:
                fields = dict(children(s.read(e.data, e.end - e.data)))
                self.scale = uint(fields.get(0x2AD7B1, b'\x0fB@'))
                self.duration = real(fields[0x4489]) * self.scale / 1000000 if 0x4489 in fields else None
                found_info = True
            elif e.id == 0x1654AE6B:
                for eid, body in children(s.read(e.data, e.end - e.data)):
                    if eid == 0xAE:
                        t = parse_track(body)
                        if t:
                            self.tracks.append(t)
                found_tracks = True
            elif e.id == 0x1F43B675 and self.first_cluster is None:
                self.first_cluster = p
            if found_info and found_tracks and self.first_cluster is not None:
                break
            if e.unknown:
                raise ExtractionError('Metadados após cluster de tamanho indefinido não são suportados.')
            p = e.end
        if not self.tracks:
            raise ExtractionError('Nenhuma faixa de legenda embutida foi encontrada neste stream.')
        if self.first_cluster is None:
            raise ExtractionError('O stream não contém blocos de mídia.')
        return self.tracks

    def _block(self, e, track):
        s = self.source
        head = s.read(e.data, min(11, e.end - e.data))
        number, n = vint(head)
        if number != track.number:
            return None
        if len(head) < n + 3:
            raise ExtractionError('Bloco de legenda truncado.')
        relative = int.from_bytes(head[n:n + 2], 'big', signed=True)
        if head[n + 2] & 0x06:
            raise ExtractionError('Legendas com lacing não são suportadas nesta versão.')
        body = s.read(e.data + n + 3, e.end - e.data - n - 3)
        for _, algorithm, prefix in sorted(track.compression, reverse=True):
            if algorithm == 3:
                body = prefix + body
            elif algorithm == 0:
                decoder = zlib.decompressobj()
                body = decoder.decompress(body, 8 * MIB)
                if not decoder.eof or decoder.unconsumed_tail:
                    raise ExtractionError('Legenda comprimida inválida ou grande demais.')
        return relative, string(body)

    def _metadata(self, eid):
        position = self.seek.get(eid)
        if position is None:
            return None
        e = element(self.source, position, self.segment.end)
        if e.id != eid or e.unknown:
            raise ExtractionError('Índice de metadados inválido.')
        if e.end - e.data > 8 * MIB:
            return None
        return self.source.read(e.data, e.end - e.data)

    def _indexed_entries(self, track):
        """Use Cues only when a per-track frame count proves the index is complete."""
        if not track.uid or 0x1C53BB6B not in self.seek or 0x1254C367 not in self.seek:
            return None
        self.method = 'Verificando índice de legendas'
        tags = self._metadata(0x1254C367)
        if tags is None:
            return None
        counts = set()
        for eid, body in children(tags):
            if eid != 0x7373:
                continue
            values = list(children(body))
            targets = [uint(v) for k, b in values if k == 0x63C0
                       for field, v in children(b) if field == 0x63C5]
            if targets != [track.uid]:
                continue
            for key, b in values:
                if key == 0x67C8:
                    fields = dict(children(b))
                    if fields.get(0x45A3) == b'NUMBER_OF_FRAMES':
                        raw = fields.get(0x4487, b'')
                        if raw.isdigit():
                            counts.add(int(raw))
        if len(counts) != 1 or not next(iter(counts)):
            return None
        data = self._metadata(0x1C53BB6B)
        if data is None:
            return None
        entries = []
        seen = set()
        for eid, body in children(data):
            if eid != 0xBB:
                continue
            values = list(children(body))
            times = [uint(v) for k, v in values if k == 0xB3]
            if len(times) != 1:
                return None
            for key, b in values:
                if key != 0xB7:
                    continue
                fields = {k: uint(v) for k, v in children(b)}
                if fields.get(0xF7) != track.number:
                    continue
                if 0xF1 not in fields or 0xF0 not in fields:
                    return None
                cluster = self.segment.data + fields[0xF1]
                offset = fields[0xF0]
                if (cluster, offset) in seen:
                    return None
                seen.add((cluster, offset))
                entries.append((cluster, offset, times[0]))
        if len(entries) != next(iter(counts)):
            return None  # Sparse/incomplete indexes must never yield a "complete" subtitle.
        return sorted(entries)

    def _extract_indexed(self, track, entries, progress):
        self.method = 'Extraindo pelo índice (4 conexões)'
        cues = []
        local = threading.local()
        def fetch(index_entry):
            if not hasattr(local, 'media'):
                local.media = copy.copy(self)
                local.media.source = self.source.fork()
            i, entry = index_entry
            return local.media._indexed_cue(track, entry, i)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        futures = [pool.submit(fetch, entry) for entry in enumerate(entries)]
        try:
            for future in concurrent.futures.as_completed(futures):
                cues.append(future.result())
                progress(len(cues) / len(entries), len(cues))
        except BaseException:
            self.source.cancel.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            for peer in self.source.peers[1:]:
                peer.session.close()
            self.source.peers[:] = [self.source]
        return sorted(cues, key=lambda c: (c.start, c.order))

    def _indexed_cue(self, track, entry, i):
        s = self.source
        cluster_pos, relative_pos, cue_time = entry
        cluster = element(s, cluster_pos, self.segment.end)
        if cluster.id != 0x1F43B675:
            raise ExtractionError('O índice aponta para um cluster inválido.')
        position = cluster.data + relative_pos
        if position >= cluster.end:
            raise ExtractionError('Posição de legenda inválida no índice.')
        # Read only a small area around the indexed subtitle, never the whole cluster.
        target = element(s, position, cluster.end, min(self.prefetch, 512))
        block = None
        duration = None
        if target.id == 0xA0 and not target.unknown:
            p = target.data
            while p < target.end:
                item = element(s, p, target.end)
                if item.unknown:
                    raise ExtractionError('Bloco indexado inválido.')
                if item.id == 0xA1:
                    block = self._block(item, track)
                elif item.id == 0x9B:
                    duration = uint(s.read(item.data, item.end - item.data))
                p = item.end
        elif target.id == 0xA3:
            block = self._block(target, track)
        if block is None:
            raise ExtractionError('O índice não corresponde à faixa selecionada. Extração interrompida.')
        length = duration * track.scale * self.scale / 1000000 if duration is not None else track.default_ns / 1000000
        if length <= 0:
            raise ExtractionError('Uma legenda indexada não informa sua duração.')
        start = cue_time * self.scale / 1000000 - track.delay_ns / 1000000
        return Cue(max(0, start), max(0, start + length), block[1], i)

    def extract(self, track, progress=None):
        if not track.usable:
            raise ExtractionError(track.unsupported or 'Formato de legenda não suportado.')
        progress = progress or (lambda *args: None)
        entries = self._indexed_entries(track)
        if entries:
            return self._extract_indexed(track, entries, progress)
        self.method = 'Varredura: este arquivo não possui índice completo verificável'
        s = self.source
        p = self.first_cluster
        cues = []
        while p < self.segment.end:
            e = element(s, p, self.segment.end, self.prefetch)
            if e.id != 0x1F43B675:
                if e.unknown:
                    raise ExtractionError('Elemento indefinido não suportado.')
                p = e.end
                continue
            q = e.data
            cluster_time = None
            pending = []
            while q < e.end:
                child = element(s, q, e.end, self.prefetch)
                # An unknown-size Cluster ends at the next Segment-level element.
                if child.id in (0x1F43B675, 0x1C53BB6B, 0x114D9B74, 0x1254C367,
                                0x1941A469, 0x1043A770, 0x1654AE6B, 0x1549A966):
                    if not e.unknown:
                        raise ExtractionError('Estrutura de cluster inválida.')
                    break
                if child.unknown:
                    raise ExtractionError('Bloco com tamanho indefinido não suportado.')
                if child.id == 0xE7:
                    cluster_time = uint(s.read(child.data, child.end - child.data))
                elif child.id == 0xA3:
                    block = self._block(child, track)
                    if block:
                        pending.append((block, None))
                elif child.id == 0xA0:
                    r = child.data
                    block = None
                    duration = None
                    while r < child.end:
                        item = element(s, r, child.end, self.prefetch)
                        if item.unknown:
                            raise ExtractionError('BlockGroup inválido.')
                        if item.id == 0xA1:
                            block = self._block(item, track)
                            # Duration belongs to this Block only. Other tracks can be skipped.
                            if block is None:
                                break
                        elif item.id == 0x9B:
                            duration = uint(s.read(item.data, item.end - item.data))
                        r = item.end
                    if block:
                        pending.append((block, duration))
                q = child.end
                progress(min(q / self.segment.end, 1), len(cues) + len(pending))
            if pending and cluster_time is None:
                raise ExtractionError('Cluster sem marcação de tempo.')
            for (relative, text), duration in pending:
                start = (cluster_time + relative * track.scale) * self.scale / 1000000 - track.delay_ns / 1000000
                length = duration * track.scale * self.scale / 1000000 if duration is not None else track.default_ns / 1000000
                if length <= 0:
                    raise ExtractionError('Uma legenda não informa a duração. A extração foi interrompida '
                                          'para não inventar os tempos das falas.')
                cues.append(Cue(max(0, start), max(0, start + length), text, len(cues)))
            p = q if e.unknown else e.end
        if not cues:
            raise ExtractionError('A faixa selecionada não contém falas.')
        return sorted(cues, key=lambda c: (c.start, c.order))


def timestamp(ms, ass=False):
    value = max(0, round(ms / 10) if ass else round(ms))
    units = 100 if ass else 1000
    seconds, fraction = divmod(value, units)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}:{minutes:02}:{seconds:02}.{fraction:02}' if ass else f'{hours:02}:{minutes:02}:{seconds:02},{fraction:03}'


def ass_fields(text):
    fields = text.split(',', 8)
    if len(fields) != 9:
        raise ExtractionError('Evento ASS/SSA inválido.')
    return fields


def ass_plain(text):
    # Ignore ASS vector drawings, keep dialogue around them, remove positioning/style tags.
    drawing = False
    output = []
    for token in re.split(r'(\{[^}]*\})', text):
        if token.startswith('{') and token.endswith('}'):
            modes = re.findall(r'\\p(\d+)(?!\d)', token)
            if modes:
                drawing = int(modes[-1]) != 0
        elif not drawing:
            output.append(token)
    return ''.join(output).replace('\\N', '\n').replace('\\n', '\n').replace('\\h', '\u00a0')


def render(cues, track, output_format='srt'):
    if output_format in ('ass', 'ssa'):
        expected = 'S_TEXT/ASS' if output_format == 'ass' else 'S_TEXT/SSA'
        if track.codec != expected:
            raise ExtractionError('Use SRT para esta faixa.')
        header = string(track.private).replace('\r\n', '\n').strip()
        if '[Events]' not in header:
            raise ExtractionError('Cabeçalho ASS/SSA ausente. Exporte em SRT.')
        header = re.sub(r'(?mi)^Dialogue:.*\n?', '', header)
        lines = [header]
        for c in cues:
            f = ass_fields(c.text)
            layer = f[1] if output_format == 'ass' else 'Marked=0'
            lines.append('Dialogue: ' + ','.join([layer, timestamp(c.start, True),
                         timestamp(c.end, True)] + f[2:]))
        return '\n'.join(lines) + '\n'
    parts = []
    for c in cues:
        text = c.text
        if track.codec in ('S_TEXT/ASS', 'S_TEXT/SSA'):
            text = ass_plain(ass_fields(text)[8])
        elif track.codec == 'S_TEXT/WEBVTT':
            # Matroska stores cue identifier and settings on the first two lines.
            fields = text.split('\n', 2)
            if len(fields) != 3:
                raise ExtractionError('Bloco WebVTT inválido.')
            text = fields[2]
            text = re.sub(r'<(?:/?(?:c|v|lang|ruby|rt))(?:[ .][^>]*)?>', '', text)
            text = re.sub(r'<\d{2}:\d{2}(?::\d{2})?\.\d{3}>', '', text)
            text = html.unescape(text)
        text = text.replace('\r\n', '\n').replace('\r', '\n').strip()
        text = re.sub(r'\n[ \t]*\n+', '\n', text)
        if not text or c.end <= c.start:
            continue
        parts.append(f'{len(parts) + 1}\n{timestamp(c.start)} --> {timestamp(c.end)}\n{text}\n')
    if not parts:
        raise ExtractionError('Não há falas de texto nesta faixa.')
    return '\n'.join(parts)


def save_atomic(path, text):
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='\r\n',
                                         prefix='.legenda-', suffix='.tmp',
                                         dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()

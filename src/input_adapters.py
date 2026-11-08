"""Timed-format adapters. Source timing is explicit; no reading-speed estimates."""
import csv
from decimal import Decimal
import hashlib
from io import BytesIO,StringIO
import json
from pathlib import PurePosixPath
import re
import zipfile
from .models import Episode,Event
from .parser import parse_text,parse_time

SUPPORTED_FORMATS={'.txt','.md','.json','.srt','.vtt','.csv','.docx','.pdf'}

class SourceFormatError(ValueError):
    def __init__(self,code,message):super().__init__(message);self.code=code


def episode_number(text,name,default_number):
    match=re.search(r'EPISODE\s+(?:NUMBER\s*:\s*(?:Episode\s*)?|NO\.?\s*:\s*)?(\d+)',text,re.I)
    match=match or re.search(r'(?:episode|ep)[-_ ]*(\d+)',PurePosixPath(name).name,re.I)
    if match is None:
        for parent in reversed(PurePosixPath(name).parts[:-1]):
            match=re.search(r'(?:episode|ep)[-_ ]*(\d+)',parent,re.I)
            if match:break
    return int(match[1]) if match else default_number


def make_episode(raw,name,number,kind):
    return Episode(episode_id=f'E{number:03d}',episode_number=number,source_file=name,
        source_sha256=hashlib.sha256(raw).hexdigest(),metadata={'input_format':kind})


def event(ep,start,end,text,line,kind='dialogue',speaker=None):
    return Event(event_id=f'{ep.episode_id}_V{len(ep.events)+1:04d}',episode_id=ep.episode_id,
        event_type=kind,start_ms=start,end_ms=end,text=text,speaker_raw=speaker,
        source_file=ep.source_file,source_line_start=line,source_line_end=line,source_order=len(ep.events))


def subtitles(raw,name,default_number):
    text=raw.decode('utf-8-sig');lines=text.splitlines()
    ep=make_episode(raw,name,episode_number('',name,default_number),'subtitles')
    for index,line in enumerate(lines):
        timing=re.match(r'\s*(\d+:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*(\d+:\d{2}(?::\d{2})?[.,]\d{1,3})',line)
        if not timing:continue
        content=[];j=index+1
        while j<len(lines) and lines[j].strip():content.append(lines[j]);j+=1
        value='\n'.join(content).strip()
        speaker=None
        voice=re.search(r'<v(?:\.[^ >]+)?\s+([^>]+)>',value)
        if voice:speaker=voice[1].strip()
        value=re.sub(r'<[^>]+>','',value).strip()
        named=re.match(r'^([A-Z][A-Z \-]{1,35}):\s*(.+)',value,re.S)
        if named:speaker=named[1];value=named[2]
        if not value:raise SourceFormatError('empty_cue','A subtitle cue has timing but no text.')
        ep.events.append(event(ep,parse_time(timing[1]),parse_time(timing[2]),value,index+1,speaker=speaker))
    if not ep.events:raise SourceFormatError('timestamps_required','No subtitle cues found. Use time --> time followed by dialogue.')
    ep.metadata['content_limit']='Dialogue/subtitle evidence only; visual actions are not inferred.'
    return [ep]


def csv_episodes(raw,name,default_number):
    reader=csv.DictReader(StringIO(raw.decode('utf-8-sig')))
    if not reader.fieldnames:raise SourceFormatError('csv_columns','CSV is empty.')
    reader.fieldnames=[x.strip().lower() for x in reader.fieldnames]
    fields=set(reader.fieldnames)
    milliseconds={'start_ms','end_ms'}<=fields
    if 'text' not in fields or not (milliseconds or {'start','end'}<=fields):
        raise SourceFormatError('csv_columns','CSV needs text and start/end (seconds or clocks), or start_ms/end_ms. Optional: episode, speaker, event_type.')
    result={}
    def time(v):
        return parse_time(v) if ':' in v else int(Decimal(v)*1000)
    for line,row in enumerate(reader,2):
        if not any(row.values()):continue
        number=int(row.get('episode') or episode_number('',name,default_number))
        ep=result.setdefault(number,make_episode(raw,name,number,'csv'))
        a=int(row['start_ms']) if milliseconds else time(row['start'])
        b=int(row['end_ms']) if milliseconds else time(row['end'])
        kind=(row.get('event_type') or 'dialogue').strip().lower()
        ep.events.append(event(ep,a,b,row['text'],line,kind,row.get('speaker') or None))
    if not result:raise SourceFormatError('empty_source','No CSV rows found.')
    return list(result.values())


def parse_source(raw,name,default_number=1):
    suffix=PurePosixPath(name).suffix.lower()
    if suffix=='.json':
        value=json.loads(raw)
        items=value if isinstance(value,list) else value.get('episodes',[value])
        return [Episode.model_validate(x) for x in items]
    if suffix in ('.srt','.vtt'):return subtitles(raw,name,default_number)
    if suffix=='.csv':return csv_episodes(raw,name,default_number)
    if suffix=='.pdf':
        from pypdf import PdfReader
        try:
            pdf=PdfReader(BytesIO(raw))
            if pdf.is_encrypted:raise SourceFormatError('encrypted_pdf','Unlock the PDF before upload.')
            if len(pdf.pages)>200:raise SourceFormatError('pdf_page_limit','PDF limit: 200 pages per file.')
            text='\n'.join(page.extract_text() or '' for page in pdf.pages)
        except SourceFormatError:raise
        except Exception:raise SourceFormatError('invalid_pdf','This PDF could not be read.') from None
        if not text.strip():raise SourceFormatError('scanned_pdf','This PDF has no extractable text. OCR it first or upload timed text/subtitles.')
    elif suffix=='.docx':
        from docx import Document
        try:
            with zipfile.ZipFile(BytesIO(raw)) as z:
                if sum(i.file_size for i in z.infolist())>16*1024*1024:raise SourceFormatError('docx_size','DOCX expanded content is too large.')
            document=Document(BytesIO(raw))
            text='\n'.join(p.text for p in document.paragraphs)
            for table in document.tables:
                text+='\n'+'\n'.join(' | '.join(c.text for c in row.cells) for row in table.rows)
        except SourceFormatError:raise
        except Exception:raise SourceFormatError('invalid_docx','This DOCX could not be read.') from None
    else:text=raw.decode('utf-8-sig')
    if len(text.encode())>8*1024*1024:raise SourceFormatError('extracted_size','Extracted text exceeds 8 MB.')
    if '-->' in text:return subtitles(text.encode(),name,default_number)
    if not re.search(r'Start\s*:',text,re.I):
        raise SourceFormatError('timestamps_required','Readable script, but no supported event timestamps. Add Start/End timing, upload SRT/VTT, or use the CSV template. Exact promo cuts cannot be inferred from reading speed.')
    # A new file without an episode header gets a stable import-order identity.
    ep=parse_text(text.encode(),name,default_number=episode_number(text,name,default_number))
    ep.source_sha256=hashlib.sha256(raw).hexdigest()
    ep.metadata['input_format']=suffix.lstrip('.')
    return [ep]

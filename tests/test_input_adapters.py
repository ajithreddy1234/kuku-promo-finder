from io import BytesIO
import json
import zipfile
from docx import Document
from src.ingestion import ingest


def test_new_filename_without_episode_header_gets_stable_identity(tmp_path):
    p=tmp_path/'another-show.txt';p.write_text('AVA:\nStart: 00:00.000 | End: 00:25.000\nDo not open that door.\n')
    eps,r=ingest(p)
    assert r['complete_ingestion'] and eps[0].episode_number==1 and eps[0].source_ready


def test_subtitle_pack_dynamic_episode_numbers_and_speaker(tmp_path):
    p=tmp_path/'subtitles.zip'
    with zipfile.ZipFile(p,'w') as z:
        z.writestr('episode-73.srt','1\n00:00:00,000 --> 00:00:25,000\nAVA: The signal came from inside.\n\n2\n00:00:25,000 --> 00:00:40,000\nWhat is behind that door?\n')
        z.writestr('episode-81.vtt','WEBVTT\n\n00:00.000 --> 00:30.000\n<v LEO>The door is opening.</v>\n')
    eps,r=ingest(p)
    assert r['complete_ingestion'] and [e.episode_number for e in eps]==[73,81]
    assert eps[0].events[0].speaker_raw=='AVA' and eps[1].events[0].speaker_raw=='LEO'
    assert eps[0].events[0].start_ms==0 and eps[1].events[0].end_ms==30000


def test_csv_can_contain_multiple_episodes_without_scenes(tmp_path):
    p=tmp_path/'new-show.csv';p.write_text('episode,start,end,speaker,event_type,text\n7,0,25,AVA,dialogue,The door is opening.\n9,00:00.000,00:35.000,,action,The radio flashes.\n')
    eps,r=ingest(p)
    assert r['complete_ingestion'] and [e.episode_number for e in eps]==[7,9]
    assert all(e.source_ready for e in eps)


def test_docx_timing_is_preserved_and_raw_hash_roundtrips(tmp_path):
    p=tmp_path/'different-story.docx';d=Document()
    for value in ['AVA:','Start: 00:00.000 | End: 00:25.000','The door is opening.']:d.add_paragraph(value)
    d.save(p);eps,r=ingest(p)
    assert r['complete_ingestion'] and eps[0].events[0].end_ms==25000
    import hashlib
    assert eps[0].source_sha256==hashlib.sha256(p.read_bytes()).hexdigest()


def test_untimed_script_is_explained_without_fabricated_timing(tmp_path):
    p=tmp_path/'untimed.txt';p.write_text('INT. ROOM\nAVA: There is someone at the door.')
    eps,r=ingest(p)
    assert not eps and r['files'][0]['code']=='timestamps_required'
    assert 'Exact promo cuts' in r['files'][0]['message']


def test_pdf_without_text_reports_ocr_requirement(tmp_path):
    from pypdf import PdfWriter
    p=tmp_path/'scanned.pdf';writer=PdfWriter();writer.add_blank_page(width=200,height=200)
    with p.open('wb') as f:writer.write(f)
    eps,r=ingest(p)
    assert not eps and r['files'][0]['code']=='scanned_pdf'
    assert 'OCR' in r['files'][0]['message']

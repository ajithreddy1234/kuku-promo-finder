import json
import zipfile
import pytest

from src.ingestion import ingest
from src.parser import parse_text, parse_time


def script(number=1):
    return f'''EPISODE NUMBER: Episode {number}
EPISODE LOGLINE: Synthetic test only.
SCENE 01
SCENE HEADING: INT. ROOM
SCENE START: 00:00.00
SCENE END: 00:30.00
**CAMERA:**
Start: (00:00.00) | End: (00:05.00)
A door opens.
**AVA (Quiet):**
Start: (00:01.00) | End: (00:04.00)
"Who is there?"
**SOUND FX:**
Start: (00:02.00) | End: (00:05.00)
A knock.
--- SCRIPT END ---
'''.encode()


@pytest.mark.parametrize('value,expected', [('01:02.34',62340),('01:02:03.005',3723005),('00:09,85',9850),('100:00',6000000)])
def test_clock(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize('value', ['00:60.00','01:75:02','-1:02','banana','00:01.0001'])
def test_invalid_clock(value):
    with pytest.raises(ValueError): parse_time(value)


def test_overlap_is_not_summed_and_source_locations_preserved():
    ep=parse_text(script(), 'episode-001.txt')
    assert len(ep.events)==3
    assert ep.duration_ms==30000  # declared scene end, not event sum
    assert [v.event_type for v in ep.events]==['camera','dialogue','sound']
    assert ep.events[1].source_line_start==10
    assert not ep.issues


def test_case_plain_headers_and_bom():
    raw=script().decode().replace('SCENE END','scene end').replace('**CAMERA:**','Camera:')
    assert len(parse_text(('\ufeff'+raw).encode(), 'episode-001.txt').events)==3


def test_malformed_event_is_reported():
    ep=parse_text(script().replace(b'00:04.00',b'00:00.50'), 'episode-001.txt')
    assert any(i.code=='invalid_event' and i.severity=='error' for i in ep.issues)


def test_order_is_preserved_separately():
    ep=parse_text(script().replace(b'(00:01.00)',b'(00:00.00)').replace(b'(00:00.00)',b'(00:02.00)',1), 'episode-001.txt')
    assert ep.events[0].event_type=='camera'
    assert ep.temporal_events()[0].event_type=='dialogue'
    assert any(i.code=='source_order_differs' for i in ep.issues)


def test_archive_and_json_roundtrip(tmp_path):
    archive=tmp_path/'input.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('nested/episode-001.txt',script())
    episodes,report=ingest(archive)
    assert report['complete_ingestion']
    canonical=tmp_path/'canonical.json'
    canonical.write_text(json.dumps([episodes[0].model_dump()]))
    again,result=ingest(canonical)
    assert result['complete_ingestion'] and again==episodes


def test_unsafe_zip_and_unsupported_files_are_visible(tmp_path):
    archive=tmp_path/'bad.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('../episode-001.txt',script())
        z.writestr('unknown.pdf',b'unknown')
    episodes,report=ingest(archive)
    assert not episodes
    assert not report['complete_ingestion']
    assert report['status_counts']['failed']==2
    assert not (tmp_path.parent/'episode-001.txt').exists()


def test_duplicate_episode_is_failure(tmp_path):
    (tmp_path/'episode-001.txt').write_bytes(script())
    (tmp_path/'duplicate.txt').write_bytes(script())
    episodes,report=ingest(tmp_path)
    assert len(episodes)==1 and not report['complete_ingestion']


def test_identity_conflict_is_visible():
    ep=parse_text(script(), 'episode-002.txt')
    assert any(i.code=='episode_identity_conflict' for i in ep.issues)


def test_orphan_timing_is_error():
    raw=script().replace(b'**AVA (Quiet):**\n', b'')
    ep=parse_text(raw, 'episode-001.txt')
    assert any(i.code=='orphan_timing' for i in ep.issues)


def test_untimed_record_is_not_silently_dropped():
    raw=script().replace(b'**SOUND FX:**\nStart: (00:02.00) | End: (00:05.00)', b'**SOUND FX:**')
    ep=parse_text(raw, 'episode-001.txt')
    assert any(i.code=='unparsed_content' for i in ep.issues)


def test_ambiguous_scene_range_marks_timing_uncertain():
    ep=parse_text(script().replace(b'SCENE START: 00:00.00',b'SCENE START: 00:02.00'), 'episode-001.txt')
    assert any(i.code=='outside_scene' for i in ep.issues)
    assert all(e.timing_status=='uncertain' for e in ep.events)


def test_unknown_json_scene_reference_rejected(tmp_path):
    ep=parse_text(script(),'episode-001.txt').model_dump()
    ep['events'][0]['scene_id']='NONEXISTENT'
    p=tmp_path/'input.json'
    p.write_text(json.dumps(ep))
    episodes,report=ingest(p)
    assert not episodes and not report['complete_ingestion']


def canonical_file(tmp_path, data):
    p=tmp_path/'canonical.json'
    p.write_text(json.dumps(data))
    return p


def test_json_recomputes_missing_quality_warnings(tmp_path):
    data=parse_text(script(),'episode-001.txt').model_dump()
    data['scenes'][0]['end_ms']=2000
    data['issues']=[]
    episodes,report=ingest(canonical_file(tmp_path,data))
    assert report['complete_ingestion']
    assert report['source_ready_episodes']==0
    assert report['requires_source_review']==['E001']
    assert all(e.timing_status=='uncertain' for e in episodes[0].events)
    assert episodes[0].scenes[0].end_ms==2000  # original conflict is not repaired


def test_empty_canonical_episode_is_not_complete(tmp_path):
    data=parse_text(script(),'episode-001.txt').model_dump()
    data['events']=[]
    _,report=ingest(canonical_file(tmp_path,data))
    assert not report['complete_ingestion']
    assert report['issue_counts']['empty_episode']==1


@pytest.mark.parametrize('mutation', ['scene','event','order'])
def test_duplicate_canonical_identity_rejected(tmp_path,mutation):
    data=parse_text(script(),'episode-001.txt').model_dump()
    if mutation=='scene': data['scenes'].append(data['scenes'][0])
    if mutation=='event': data['events'][1]['event_id']=data['events'][0]['event_id']
    if mutation=='order': data['events'][1]['source_order']=data['events'][0]['source_order']
    episodes,report=ingest(canonical_file(tmp_path,data))
    assert not episodes and not report['complete_ingestion']


def test_json_list_order_does_not_override_provenance(tmp_path):
    data=parse_text(script(),'episode-001.txt').model_dump()
    data['events'].reverse()
    episodes,report=ingest(canonical_file(tmp_path,data))
    assert report['source_ready_episodes']==1
    assert episodes[0].original_events()[0].event_type=='camera'
    assert episodes[0].temporal_events()[0].event_type=='camera'
    assert 'source_order_differs' not in report['issue_counts']


@pytest.mark.parametrize('status',['derived','uncertain'])
def test_nonexplicit_imported_timing_is_review_only(tmp_path,status):
    data=parse_text(script(),'episode-001.txt').model_dump()
    data['events'][0]['timing_status']=status
    episodes,report=ingest(canonical_file(tmp_path,data))
    assert report['complete_ingestion'] and not episodes[0].source_ready


@pytest.mark.parametrize('value',[True,1.2,'1000'])
def test_canonical_timestamps_must_be_integer_milliseconds(tmp_path,value):
    data=parse_text(script(),'episode-001.txt').model_dump()
    data['events'][0]['start_ms']=value
    episodes,report=ingest(canonical_file(tmp_path,data))
    assert not episodes and not report['complete_ingestion']


def test_empty_content_rejected_in_canonical_import(tmp_path):
    data=parse_text(script(),'episode-001.txt').model_dump()
    data['events'][0]['text']='   '
    assert not ingest(canonical_file(tmp_path,data))[1]['complete_ingestion']


def test_duplicate_episode_numbers_rejected_even_with_different_ids(tmp_path):
    one=parse_text(script(),'episode-001.txt').model_dump()
    two=parse_text(script(),'episode-001.txt').model_dump()
    two['episode_id']='OTHER'
    for e in two['events']: e['episode_id']='OTHER'
    assert not ingest(canonical_file(tmp_path,[one,two]))[1]['complete_ingestion']


def test_filename_identity_uses_basename():
    ep=parse_text(script(),'episode-999-folder/episode-001.txt')
    assert not any(i.code=='episode_identity_conflict' for i in ep.issues)


def test_bold_episode_metadata_is_recognized():
    raw=script().replace(b'EPISODE NUMBER: Episode 1',b'**EPISODE NUMBER: Episode 1**')
    ep=parse_text(raw,'arbitrary-name.txt')
    assert ep.episode_number==1 and ep.source_ready


def test_words_script_end_in_dialogue_do_not_stop_parsing():
    raw=script().replace(b'Who is there?',b'I saw SCRIPT END on the screen.')
    ep=parse_text(raw,'episode-001.txt')
    assert len(ep.events)==3 and ep.source_ready


def test_bad_encoding_is_reported(tmp_path):
    p=tmp_path/'episode-001.txt'
    p.write_bytes(b'\xff\xfeinvalid')
    assert ingest(p)[1]['status_counts']['failed']==1


def test_incomplete_scene_metadata_is_visible():
    ep=parse_text(script().replace(b'SCENE START: 00:00.00\n',b''),'episode-001.txt')
    assert any(i.code=='missing_scene_timing' for i in ep.issues)


def test_invalid_scene_range_blocks_normal_candidates(tmp_path):
    data=parse_text(script(),'episode-001.txt').model_dump()
    data['scenes'][0]['start_ms']=10000
    data['scenes'][0]['end_ms']=5000
    episodes,report=ingest(canonical_file(tmp_path,data))
    assert not episodes[0].source_ready and not report['complete_ingestion']
    assert report['issue_counts']['invalid_scene_interval']==1


def test_source_hash_matches_actual_input_bytes(tmp_path):
    import hashlib
    p=tmp_path/'episode-001.txt'
    p.write_bytes(script())
    report=ingest(p)[1]
    assert report['input_hash']==hashlib.sha256(p.read_bytes()).hexdigest()


def test_archive_entry_limit_is_enforced(tmp_path,monkeypatch):
    import src.ingestion as module
    monkeypatch.setattr(module,'MAX_FILES',1)
    p=tmp_path/'pack.zip'
    with zipfile.ZipFile(p,'w') as z:
        z.writestr('episode-001.txt',script())
        z.writestr('episode-002.txt',script(2))
    with pytest.raises(ValueError): ingest(p)


def test_oversize_archive_file_is_reported(tmp_path,monkeypatch):
    import src.ingestion as module
    monkeypatch.setattr(module,'MAX_FILE_BYTES',10)
    p=tmp_path/'pack.zip'
    with zipfile.ZipFile(p,'w') as z: z.writestr('episode-001.txt',script())
    report=ingest(p)[1]
    assert report['files'][0]['code']=='file_size_limit'
    assert not report['complete_ingestion']


def test_corrupt_zip_has_clean_cli_failure(tmp_path,monkeypatch,capsys):
    import run
    p=tmp_path/'broken.zip'
    p.write_bytes(b'not a zip')
    monkeypatch.setattr(run,'__file__',str(tmp_path/'run.py'))
    monkeypatch.setattr('sys.argv',['run.py','--input',str(p),'--output',str(tmp_path/'runs'/'test')])
    assert run.main()==2
    assert 'BadZipFile' in capsys.readouterr().err


def test_cli_refuses_overwrite(tmp_path,monkeypatch):
    import run
    output=tmp_path/'runs'/'existing'
    output.mkdir(parents=True)
    sentinel=output/'keep.txt'
    sentinel.write_text('do not overwrite')
    monkeypatch.setattr(run,'__file__',str(tmp_path/'run.py'))
    monkeypatch.setattr('sys.argv',['run.py','--input','unused','--output',str(output)])
    with pytest.raises(SystemExit): run.main()
    assert sentinel.read_text()=='do not overwrite'

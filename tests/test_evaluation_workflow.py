from datetime import datetime,timezone
from pathlib import Path
import pytest
import yaml
from src.parser import parse_text
from src.evaluation import reference_template,validate_references,require_held_out_access,config_hash
from tools.evaluate import temporal_recovery

ROOT=Path(__file__).resolve().parents[1]


def fixture():
    config=yaml.safe_load((ROOT/'config.yaml').read_text())
    config['evaluation'].update(development_episode_numbers=[1,6,12],tuning_episode_numbers=[3,25,50],held_out_episode_numbers=[2,9,18,27,36,45],primary_reference_episode_numbers=[2,18,36])
    source=(ROOT/'data/examples/synthetic-episode-001.txt').read_text()
    eps=[parse_text(source.replace('Episode 1',f'Episode {n}').encode(),f'episode-{n:03d}.txt')
         for n in config['evaluation']['held_out_episode_numbers']]
    bundle=reference_template(eps,'synthetic',config)
    bundle.evaluator='Synthetic test evaluator';bundle.recorded_at=datetime.now(timezone.utc)
    bundle.independent_before_system_outputs=True
    for e in bundle.entries:
        if int(e.episode_id[1:]) in config['evaluation']['primary_reference_episode_numbers']:
            e.decision='none';e.reason='Synthetic test with no picks'
    return config,eps,bundle


def test_primary_three_complete_optional_still_locked():
    config,eps,bundle=fixture()
    validate_references(bundle,eps,'synthetic',config)
    require_held_out_access(eps[0],eps,'synthetic',config,bundle,config_hash(config))
    optional=next(e for e in eps if e.episode_number==9)
    with pytest.raises(ValueError,match='pending'):require_held_out_access(optional,eps,'synthetic',config,bundle,config_hash(config))


def test_completed_labels_do_not_bypass_freeze():
    config,eps,bundle=fixture()
    with pytest.raises(ValueError,match='frozen'):require_held_out_access(eps[0],eps,'synthetic',config,bundle)


def test_no_reference_opportunities_has_undefined_recovery():
    config,eps,bundle=fixture()
    value=temporal_recovery(bundle,[],config['evaluation']['primary_reference_episode_numbers'],eps)
    assert value['temporal_sample_recovery'] is None
    assert value['editorially_confirmed_recovery'] is None

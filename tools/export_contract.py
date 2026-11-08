"""Export public data contracts and a synthetic example, never real scripts."""
import argparse
import json
from pathlib import Path

from src.models import Episode, Event
from src.parser import parse_text
from src.candidates import StorySignal, Candidate
from src.timeline import ClipEvent
from src.review_models import StoryAnalysis, BlindReview, ContextReview

SYNTHETIC = b'''EPISODE NUMBER: Episode 1
SCENE 01
Scene heading: INT. EMPTY ROOM - DAY
Scene start: 00:00.00
Scene end: 00:25.00
**CAMERA:**
Start: (00:00.00) | End: (00:04.00)
A close-up of a locked box.
**PERSON A:**
Start: (00:01.00) | End: (00:03.00)
"Who left this here?"
**ACTION:**
Start: (00:03.00) | End: (00:05.00)
A hand reaches for the box.
--- SCRIPT END ---
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    contracts = {'schema_version': '0.2.0', 'event': Event.model_json_schema(),
                 'episode': Episode.model_json_schema(), 'story_signal': StorySignal.model_json_schema(),
                 'candidate': Candidate.model_json_schema(), 'clip_event': ClipEvent.model_json_schema(),
                 'story_analysis': StoryAnalysis.model_json_schema(),'blind_review':BlindReview.model_json_schema(),
                 'context_review':ContextReview.model_json_schema()}
    (args.output/'Promo-Finder-Data-Contracts.json').write_text(json.dumps(contracts,indent=2)+'\n')
    example = parse_text(SYNTHETIC, 'synthetic-episode-001.txt')
    (args.output/'Promo-Finder-Synthetic-Episode.json').write_text(example.model_dump_json(indent=2)+'\n')
    print('Exported schema definitions and synthetic normalized episode. No real screenplay content used.')


if __name__=='__main__':
    main()

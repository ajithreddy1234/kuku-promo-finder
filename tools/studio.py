"""One-command local React product launcher. Never binds the private API publicly."""
from pathlib import Path
import subprocess
import uvicorn


def main():
    root=Path(__file__).resolve().parents[1]
    web=root/'web';dist=web/'dist/index.html'
    sources=[web/'package-lock.json',web/'index.html',*web.joinpath('src').rglob('*')]
    stale=not dist.exists() or any(f.is_file() and f.stat().st_mtime>dist.stat().st_mtime for f in sources)
    if stale:
        if not (web/'node_modules').exists():subprocess.run(['npm','ci'],cwd=web,check=True)
        subprocess.run(['npm','run','build'],cwd=web,check=True)
    print('Open http://127.0.0.1:8000/ — private local React workspace.',flush=True)
    uvicorn.run('api:app',host='127.0.0.1',port=8000,app_dir=str(root))


if __name__=='__main__':main()

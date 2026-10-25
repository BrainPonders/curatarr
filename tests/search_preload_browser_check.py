"""Neighbour prefetch, per-search isolation and right-side list status in real UI."""
import argparse
from dataclasses import replace
from pathlib import Path
import socket
import threading
import time

from playwright.sync_api import sync_playwright, expect
import uvicorn
from curatarr.domain import MediaIdentity
from curatarr.web.app import create_app
from test_miniapp import MiniAppTests, signed_data
from test_telegram_service import _FakeMetadataAdapter


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    fixture=MiniAppTests();fixture.setUp()
    candidates=tuple(replace(fixture.candidate,title='Movie '+str(n),identities=frozenset({MediaIdentity('tmdb',str(558+n))}),tmdb_rating=7+n/10) for n in range(3))
    metadata=_FakeMetadataAdapter(candidates)
    class Playback:
        def playback_facts(self,candidate):
            return {'status':'not_present','versions':[],'version_count':0}
    service=replace(fixture.service,metadata_adapter=metadata)
    app=create_app(fixture.config,service=service,playback_adapter=Playback())
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='error'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started:
            if time.monotonic()>deadline:raise RuntimeError('Fixture startup failed')
            time.sleep(.02)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chromium')
            for width,height in [(320,640),(390,844),(844,390),(1280,900)]:
                context=browser.new_context(viewport={'width':width,'height':height},color_scheme='dark' if width in {390,844} else 'light')
                page=context.new_page();errors=[];responses=[];requests=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('response',lambda r:responses.append(r.url))
                page.on('request',lambda r:requests.append(r.url))
                page.route('https://telegram.org/js/*',lambda r:r.fulfill(body=''))
                page.route('https://image.tmdb.org/**',lambda r:r.fulfill(path=str((Path(__file__).resolve().parent / 'fixtures' / 'poster.png').resolve()),content_type='image/png'))
                page.add_init_script('window.Telegram={WebApp:{initData:'+repr(signed_data())+',ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}}}')
                page.goto(f'http://127.0.0.1:{port}');page.set_default_timeout(10000)
                page.locator('#query').fill('Movie');page.locator('#search-submit').click()
                expect(page.locator('#movie-title')).to_have_text('Movie 0')
                # New placeholders have no title; title!=Checking does not prove a read completed.
                expect(page.locator('.list-status')).to_have_text(['Unknown'] * 3, timeout=10000)
                first=[u for u in requests if '/api/search/' in u and u.endswith('/status')]
                assert len(first)==3,first
                page.locator('#next').click();expect(page.locator('#movie-title')).to_have_text('Movie 1')
                expect(page.locator('#availability')).to_have_text('Unknown')
                expect(page.locator('#tmdb-rating')).to_contain_text('71%')
                page.locator('#previous').click();expect(page.locator('#tmdb-rating')).to_contain_text('70%')
                assert len([u for u in requests if '/api/search/' in u and u.endswith('/status')])==3
                page.locator('#list-mode').click();expect(page.locator('.list-status')).to_have_count(3)
                for node in page.locator('.list-item').all():
                    status=node.locator('.list-status').bounding_box();poster=node.locator('img').bounding_box()
                    assert status['x']>=poster['x']+poster['width'],(status,poster)
                    title=node.locator('.list-movie-info').bounding_box()
                    assert title['x']+title['width']<=status['x'],(title,status)
                    assert node.evaluate("el=>el.lastElementChild.classList.contains('list-status')")
                    expect(node.locator('.list-status')).to_have_text('Unknown')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(args.output/f'list-status-{width}.png'))
                # A new search must not inherit the old search's cached facts.
                metadata.candidates=(replace(candidates[0],title='Fresh movie',tmdb_rating=5),)
                page.locator('#query').fill('Fresh');page.locator('#search-submit').click()
                expect(page.locator('#movie-title')).to_have_text('Fresh movie');expect(page.locator('#tmdb-rating')).to_contain_text('50%')
                expect(page.locator('.list-item')).to_have_count(1)
                assert not errors,errors
                metadata.candidates=candidates
                context.close();print('Preload/list passed:',width,height,'3 initial reads; 0 additional reads for cached next/previous',flush=True)
            browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close();fixture.client.close()


if __name__=='__main__':main()

"""Isolated real Mini App acquisition journeys; no provider or real Telegram writes."""
import argparse
from dataclasses import replace
from pathlib import Path
import socket
import tempfile
import threading
import time

from playwright.sync_api import sync_playwright, expect
import uvicorn

from curatarr.services.acquisition import MovieAcquisition
from curatarr.web.app import create_app
from test_miniapp import MiniAppTests, signed_data
from test_movie_acquisition import Catalogue, Arr


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    fixture=MiniAppTests();fixture.setUp()
    config=replace(fixture.config,coordinator=replace(fixture.config.coordinator,enabled=True))
    cat,arr=Catalogue(),Arr()
    arr.profile['name']='Movies · 4K English and French with a long profile name'
    with tempfile.TemporaryDirectory() as temp:
        app=create_app(config,database=Path(temp)/'state.sqlite',service=fixture.service,
                       acquisition_factory=lambda config,storage:MovieAcquisition(storage,cat,arr))
        listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='error'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                if time.monotonic()>deadline:raise RuntimeError('Fixture server failed to start')
                time.sleep(.02)
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chromium')
                try:
                    for width,height in [(320,640),(390,844),(844,390),(1280,900)]:
                        cat.value=None;cat.marked=False;cat.calls=[];arr.value=None;arr.calls=[]
                        context=browser.new_context(viewport={'width':width,'height':height},color_scheme='dark' if width in {390,844} else 'light')
                        page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        page.route('https://telegram.org/js/*',lambda route:route.fulfill(body=''))
                        page.add_init_script('window.Telegram={WebApp:{initData:'+repr(signed_data())+',ready(){},expand(){},BackButton:{show(){},hide(){},onClick(){}}}};')
                        page.goto(f'http://127.0.0.1:{port}');expect(page.locator('#search-submit')).to_be_enabled()
                        page.locator('#query').fill('Example');page.locator('#search-form').evaluate('(node)=>node.requestSubmit()')
                        expect(page.locator('#select')).to_be_visible();page.locator('#select').click()
                        expect(page.locator('#acquisition-actions')).to_be_hidden()
                        expect(page.locator('#acquisition-add')).to_have_count(0);expect(page.locator('#acquisition-catalogue')).to_have_count(0)
                        page.locator('#back').click();expect(page.locator('#result-add')).to_be_enabled();page.locator('#result-add').click()
                        page.locator('#acquisition-mode-catalogue').click();expect(page.locator('#acquisition-confirm')).to_be_enabled()
                        expect(page.locator('#acquisition-choices')).to_be_hidden()
                        page.screenshot(path=str(args.output/f'catalogue-{width}.png'))
                        page.locator('#acquisition-confirm').click();expect(page.locator('#acquisition-dialog')).not_to_be_visible()
                        expect(page.locator('#result-acquisition-notice')).to_contain_text('Saved successfully',timeout=10000)
                        assert arr.calls==[] and set(cat.value['collections'])=={'Library','Curatarr'}
                        page.locator('#query').fill('Example');page.locator('#search-form').evaluate('(node)=>node.requestSubmit()')
                        expect(page.locator('#result-add')).to_be_enabled();page.locator('#result-add').click()
                        expect(page.locator('#acquisition-confirm')).to_be_enabled()
                        page.locator('#acquisition-profile-buttons button[data-profile="2"]').click();page.locator('#acquisition-root').select_option('1')
                        expect(page.locator('#acquisition-confirm')).to_have_text('Confirm add and search')
                        assert arr.calls==[]
                        assert page.locator('#acquisition-dialog').evaluate('(node)=>node.getBoundingClientRect().left>=0 && node.getBoundingClientRect().right<=window.innerWidth')
                        assert page.locator('#acquisition-profile-buttons').evaluate('(node)=>node.getBoundingClientRect().right<=window.innerWidth')
                        page.screenshot(path=str(args.output/f'profile-{width}.png'))
                        page.locator('#acquisition-confirm').click();expect(page.locator('#acquisition-dialog')).not_to_be_visible()
                        expect(page.locator('#result-acquisition-notice')).to_contain_text('Saved successfully',timeout=10000)
                        assert arr.calls==['add','search'];assert not errors,errors
                        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                        page.screenshot(path=str(args.output/f'completed-{width}.png'));context.close()
                        print('Acquisition journeys passed:',width,height,flush=True)
                finally:browser.close()
        finally:server.should_exit=True;thread.join(timeout=10)


if __name__=='__main__':main()

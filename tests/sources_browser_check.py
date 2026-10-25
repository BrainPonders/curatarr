"""Real source/suggestion screens against isolated fixture providers and storage."""
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
from curatarr.services.source_intake import DiscoveryFilters
from curatarr.services.sources import MovieSources
from curatarr.storage import Storage
from curatarr.web.app import create_app
from test_miniapp import MiniAppTests, signed_data
from test_movie_acquisition import Catalogue, Arr
from test_movie_sources import Metadata
from test_source_intake import movie


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    fixture=MiniAppTests();fixture.setUp();config=replace(fixture.config,coordinator=replace(fixture.config.coordinator,enabled=True),
        users=replace(fixture.config.users,admins=(1,3,4,5)))
    cat,arr,metadata=Catalogue(),Arr(),Metadata()
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'state.sqlite'
        acquisition=lambda config,storage:MovieAcquisition(storage,cat,arr)
        sources=lambda config,storage,acq:MovieSources(storage,metadata,acq,lambda:set())
        app=create_app(config,database=path,service=fixture.service,acquisition_factory=acquisition,source_factory=sources)
        listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='error'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                if time.monotonic()>deadline:raise RuntimeError('Fixture server failed')
                time.sleep(.02)
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chromium')
                try:
                    for user,(width,height) in zip((1,3,4,5),[(320,640),(390,844),(844,390),(1280,900)]):
                        storage=Storage(path)
                        with storage.connection:
                            for table in ['coordination_step','wanted_resolution','wanted_request','coordination_operation','discovery_provenance','discovery_candidate','intake_poll','coordination_event','coordination_source']:
                                storage.connection.execute('DELETE FROM '+table)
                            storage.connection.execute("DELETE FROM state_snapshot WHERE source='wanted_selection'")
                        cat.value=None;cat.marked=False;cat.calls=[];arr.value=None;arr.calls=[];metadata.missing=False
                        src=sources(config,storage,acquisition(config,storage));source=src.discovery_source()
                        src.intake.discovery(src.intake.begin(source['id']),[movie(i,title='A very long movie title <script>no execution</script>',poster_path='/fixture.jpg') for i in range(558,570)],DiscoveryFilters(),known_keys=set())
                        storage.close()
                        context=browser.new_context(viewport={'width':width,'height':height},color_scheme='dark' if width in {390,844} else 'light')
                        page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                        page.add_init_script('window.Telegram={WebApp:{initData:'+repr(signed_data(user))+',ready(){},expand(){},BackButton:{show(){},hide(){},onClick(){}},onEvent(){}}};')
                        page.route('https://telegram.org/**',lambda route:route.fulfill(status=200,body=''))
                        poster=(Path(__file__).resolve().parent / 'fixtures' / 'poster.png')
                        page.route('https://image.tmdb.org/**',lambda route:route.fulfill(content_type='image/png',body=poster.read_bytes()))
                        page.goto(f'http://127.0.0.1:{port}');expect(page.locator('#library-navigation')).to_be_visible()
                        page.locator('#library-suggestions').click();expect(page.locator('.proposal-card')).to_have_count(10)
                        page.locator('#suggestions-continue').click();expect(page.locator('.proposal-card')).to_have_count(2)
                        page.locator('#suggestions-refresh').click();expect(page.locator('.proposal-card')).to_have_count(10)
                        page.locator('.proposal-card').first.get_by_role('button',name='No',exact=True).click()
                        expect(page.locator('.proposal-card')).to_have_count(10)
                        page.locator('#suggestions-view').select_option('declined');expect(page.locator('.proposal-card')).to_have_count(1)
                        page.locator('.proposal-card').first.get_by_role('button',name='Reconsider').click();expect(page.locator('.proposal-card')).to_have_count(0)
                        page.locator('#suggestions-view').select_option('pending');expect(page.locator('.proposal-card')).to_have_count(10)
                        page.screenshot(path=str(args.output/f'suggestions-{width}.png'))
                        page.locator('.proposal-card').first.get_by_role('button',name='Yes',exact=True).click()
                        expect(page.locator('#detail')).to_be_visible();expect(page.locator('#acquisition-dialog')).to_be_visible()
                        page.locator('#acquisition-mode-catalogue').click();expect(page.locator('#acquisition-dialog')).to_be_visible()
                        page.locator('#acquisition-cancel').click();page.locator('#back').click()
                        expect(page.get_by_role('button',name='Review choices',exact=True)).to_be_visible()
                        page.get_by_role('button',name='Review choices',exact=True).click();expect(page.locator('#acquisition-dialog')).to_be_visible()
                        page.locator('#acquisition-mode-catalogue').click();page.locator('#acquisition-confirm').click()
                        expect(page.locator('#acquisition-result')).to_contain_text('Saved successfully',timeout=10000)
                        self_calls=list(cat.calls);assert arr.calls==[]
                        page.locator('#back').click();expect(page.locator('.proposal-card')).to_have_count(10)
                        page.locator('#maintenance-toggle').click();page.locator('#settings-branch').click()
                        expect(page.locator('#source-settings')).to_be_visible();expect(page.locator('#discovery-months')).to_have_value('18')
                        page.locator('#source-settings').screenshot(path=str(args.output/f'source-groups-{width}.png'))
                        page.locator('#discovery-enabled').check();page.locator('#discovery-form').get_by_role('button',name='Save filters').click()
                        expect(page.locator('#source-settings-notice')).to_have_text('Saved.')
                        page.locator('#discovery-genres').locator('..').locator('summary').click()
                        page.locator('#discovery-genres input[value="18"]').check()
                        page.locator('#discovery-form').get_by_role('button',name='Save filters').click();expect(page.locator('#source-settings-notice')).to_have_text('Saved.')
                        page.locator('#source-settings h2').scroll_into_view_if_needed()
                        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                        page.screenshot(path=str(args.output/f'discovery-settings-{width}.png'))
                        page.locator('#wanted-settings summary').click();page.locator('#wanted-user').fill('ur123')
                        page.locator('#wanted-mode').select_option('catalogue_only');page.locator('#wanted-enabled').check()
                        page.locator('#wanted-form').get_by_role('button',name='Save wanted policy').click();expect(page.locator('#wanted-import')).to_be_enabled()
                        page.locator('#wanted-user').scroll_into_view_if_needed()
                        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                        page.screenshot(path=str(args.output/f'wanted-settings-{width}.png'))
                        page.locator('#wanted-file').set_input_files({'name':'watchlist.csv','mimeType':'text/csv','buffer':b'Const,Title\ntt0000001,Historical\n'})
                        page.locator('#wanted-count').fill('1');page.locator('#wanted-complete').check();page.locator('#wanted-import').click()
                        expect(page.locator('#source-settings-notice')).to_contain_text('historical entries protected')
                        assert arr.calls==[] and cat.calls==self_calls
                        page.screenshot(path=str(args.output/f'settings-{width}.png'))
                        page.locator('#maintenance-toggle').click();page.locator('#library-suggestions').click();expect(page.locator('.proposal-card')).to_have_count(0)
                        storage=Storage(path);src=sources(config,storage,acquisition(config,storage));source=src.wanted_source()
                        src.import_wanted(source['id'],revision=source['revision'],content='Const\ntt0000001\ntt0000002\n',count=2,complete=True,actor='fixture')
                        metadata.missing=True;src.wanted_tick();storage.close()
                        page.locator('#library-requests').click();expect(page.locator('.proposal-card')).to_have_count(1)
                        expect(page.get_by_role('button',name='Review choices',exact=True)).to_be_disabled()
                        page.screenshot(path=str(args.output/f'requests-{width}.png'))
                        page.get_by_role('button',name='Withdraw').click();expect(page.locator('.proposal-card')).to_have_count(0)
                        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                        assert not errors,errors
                        context.close();print('Source journeys passed:',width,height,flush=True)
                finally:browser.close()
        finally:server.should_exit=True;thread.join(timeout=10)


if __name__=='__main__':main()

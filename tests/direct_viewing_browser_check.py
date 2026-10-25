"""Jellyfin settings and local-before-native watch journeys at supported viewports."""
import argparse
from dataclasses import replace
from contextlib import closing
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace

from playwright.sync_api import sync_playwright, expect
import uvicorn

from curatarr.services.acquisition import MovieAcquisition
from curatarr.services.sources import MovieSources
from curatarr.services.direct_viewing import DirectViewing, CandidateViewingAdapter
from curatarr.services.jellyfin_viewing import JellyfinViewingIntake
from curatarr.services.viewing_workflow import ViewingWorkflow, bot_key
from curatarr.storage import Storage
from curatarr.storage.viewing import ViewingStore
from curatarr.web.app import create_app
import test_miniapp as mini
from test_movie_acquisition import Catalogue, Arr
from test_movie_sources import Metadata
from test_direct_viewing import Native, Completion, Verifier, USER, payload


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--dark',action='store_true');args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    fixture=mini.MiniAppTests();fixture.setUp()
    config=replace(fixture.config,coordinator=replace(fixture.config.coordinator,enabled=True),
                   jellyfin=replace(fixture.config.jellyfin,viewing_webhook_token='a'*43))
    cat,arr,metadata=Catalogue(),Arr(),Metadata()
    native=Native(cat);completion=Completion(cat);completion.lost=True
    def viewing(config,storage,**kwargs):
        direct=DirectViewing(storage,cat,completion,native,participants=[1,2])
        return ViewingWorkflow(storage,CandidateViewingAdapter(native,direct),SimpleNamespace(token=mini.TOKEN),participants=[1,2],direct=direct)
    intake=lambda config,storage:JellyfinViewingIntake(storage,Verifier())
    acq=lambda config,storage:MovieAcquisition(storage,cat,arr)
    sources=lambda config,storage,acquisition:MovieSources(storage,metadata,acquisition,lambda:set())
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'state.sqlite'
        app=create_app(config,database=path,service=fixture.service,viewing_factory=viewing,intake_factory=intake,
                       acquisition_factory=acq,source_factory=sources)
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
                    for width,height in [(320,640),(390,844),(844,390),(1280,900)]:
                        with closing(Storage(path)) as storage:
                            # Independent ordinary user/provider evidence per viewport.
                            with storage.connection:
                                storage.connection.execute("UPDATE pending_decision SET status='resolved'")
                                storage.connection.execute('DELETE FROM playback_candidate')
                                storage.connection.execute('DELETE FROM playback_session')
                                storage.connection.execute('DELETE FROM coordination_step')
                                storage.connection.execute('DELETE FROM viewing_event')
                                storage.connection.execute('DELETE FROM coordination_operation')
                                storage.connection.execute('DELETE FROM coordination_event')
                                storage.connection.execute("DELETE FROM state_snapshot WHERE media_key='jellyfin_viewing'")
                            cat.value=None;cat.calls=[];completion.calls=[]
                            ViewingStore(storage).bind({'enabled':True,'source':native.source,'bot_key':bot_key(mini.TOKEN),'chat_id':10})
                        errors=[]
                        context=browser.new_context(viewport={'width':width,'height':height},color_scheme='dark' if args.dark else 'light')
                        context.route('https://telegram.org/js/*',lambda route:route.fulfill(body=''))
                        context.add_init_script('window.Telegram={WebApp:{initData:'+repr(mini.signed_data(1))+',ready(){},expand(){},BackButton:{show(){},hide(){},onClick(){}}}};')
                        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)));page.set_default_timeout(10000)
                        page.goto(f'http://127.0.0.1:{port}')
                        page.locator('#maintenance-toggle').click();page.locator('#settings-branch').click()
                        expect(page.locator('#direct-viewing-settings')).to_be_visible()
                        expect(page.locator('#viewing-minimum')).to_have_value('5');expect(page.locator('#viewing-completed')).to_have_value('95')
                        expect(page.locator('#direct-viewing-enabled')).not_to_be_checked()
                        # Wrapped labels must not move one input below its neighbours.
                        boxes=page.locator('#discovery-form .source-fields input').evaluate_all('(els)=>els.map(el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}})')
                        axis='y' if width>360 else 'x'
                        assert max(b[axis] for b in boxes)-min(b[axis] for b in boxes)<1,boxes
                        assert max(b['height'] for b in boxes)-min(b['height'] for b in boxes)<1,boxes
                        thresholds=page.locator('#direct-viewing-form .source-fields input').evaluate_all('(els)=>els.map(el=>el.getBoundingClientRect().y)')
                        assert max(thresholds)-min(thresholds)<1,thresholds

                        page.locator('#viewing-accounts input').first.check();page.locator('#direct-viewing-enabled').check()
                        page.get_by_role('button',name='Save viewing settings',exact=True).click()
                        expect(page.locator('#direct-viewing-notice')).to_have_text('Saved.')
                        page.locator('#viewing-info').click();expect(page.locator('#viewing-explanation')).to_be_visible()
                        page.locator('#viewing-info').click();expect(page.locator('#viewing-explanation')).to_be_hidden()
                        page.screenshot(path=str(args.output/f'settings-{width}.png'),full_page=True)
                        page.locator('#source-settings').screenshot(path=str(args.output/f'sources-{width}.png'))
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        with closing(Storage(path)) as storage:
                            service=intake(config,storage);service.receive(payload('Stop',100,session=str(width)));service.tick()
                            viewing(config,storage).direct.publish()
                        page.locator('#watch-check-in').click()
                        expect(page.get_by_role('button',name='Yes, I watched this',exact=True)).to_be_visible()
                        page.screenshot(path=str(args.output/f'candidate-{width}.png'),full_page=True)
                        page.get_by_role('button',name='Yes, I watched this',exact=True).click()
                        expect(page.get_by_role('heading',name='Viewing confirmation saved',exact=True)).to_be_visible()
                        expect(page.get_by_role('button',name='Check saved confirmation',exact=True)).to_be_visible()
                        expect(page.get_by_role('button',name='No rating',exact=True)).to_have_count(0)
                        page.screenshot(path=str(args.output/f'pending-{width}.png'),full_page=True)
                        with closing(Storage(path)) as storage:
                            workflow=viewing(config,storage);workflow.direct.journal.clock=lambda:time.time()+301
                            workflow.direct.tick()
                        page.locator('#viewing-refresh').click()
                        expect(page.get_by_role('button',name='No rating',exact=True)).to_be_visible()
                        expect(page.get_by_role('heading',name='Choose a rating',exact=True)).to_be_visible()
                        page.screenshot(path=str(args.output/f'rating-{width}.png'),full_page=True)
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                        assert len(completion.calls)==1 and len(cat.value['history'])==1
                        assert not errors,errors
                        context.close();print('Direct viewing journeys passed:',width,height,flush=True)
                finally:browser.close()
        finally:server.should_exit=True;thread.join(timeout=10);fixture.doCleanups()


if __name__=='__main__':main()

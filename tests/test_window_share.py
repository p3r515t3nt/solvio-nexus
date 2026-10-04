"""N6: real temporary HTTPS, existing sessions, video-only ephemeral signaling."""
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from aiohttp import WSMsgType, WSServerHandshakeError
from test_nexus_dashboard import world
from solvio.dashboard import window_share as W

PATH = '/v1/dashboard/window'
OFFER = 'v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=sendonly\r\n'
ANSWER = OFFER.replace('sendonly','recvonly')


async def message(ws, kind):
    async with asyncio.timeout(4):
        while True:
            row = await ws.receive()
            require_equal(row.type,WSMsgType.TEXT)
            data=json.loads(row.data)
            if data['type']==kind:return data
            require_equal(data['type'],'lease')


async def closed(ws):
    async with asyncio.timeout(4):
        while True:
            row=await ws.receive()
            if row.type in {WSMsgType.CLOSE,WSMsgType.CLOSED,WSMsgType.ERROR}:return


async def connect(w, role, channel='obsidian', *, client=None, headers=None):
    ws=await (client or w.client).ws_connect(PATH,headers={'Origin':w.origin})
    await ws.send_json({'type':'hello','role':role,'channel':channel,
                       'csrf':(headers or w.headers)['X-CSRF-Token']})
    return ws


async def peer(w,role,channel='obsidian',**kw):
    ws=await connect(w,role,channel,**kw)
    return ws,(await message(ws,'ready'))['peer']


async def t_https_owner_origin_and_same_session_csrf_are_required_before_join():
    async with world() as w:
        guest=await w.new_client();foreign=await w.new_client();await w.login(foreign,'foreign')
        for client,headers in [(guest,{'Origin':w.origin}),(foreign,{'Origin':w.origin}),
                               (w.client,{}),(w.client,{'Origin':'https://foreign.invalid'})]:
            try:await client.ws_connect(PATH,headers=headers)
            except WSServerHandshakeError as exc:require_equal(exc.status,401)
            else:raise AssertionError('untrusted upgrade accepted')
        other=await w.new_client();other_headers=await w.login(other)
        ws=await connect(w,'publisher',headers=other_headers)
        await closed(ws)
        require_equal(w.app[W.HUB].peers,{})
        require_equal(w.ledger.recent_runs(),[])


async def t_real_socket_relays_only_bound_offer_and_answer_without_task_or_storage():
    async with world() as w:
        pub,pid=await peer(w,'publisher')
        a,aid=await peer(w,'viewer');require_equal((await message(pub,'viewer_joined'))['peer'],aid)
        b,bid=await peer(w,'viewer');require_equal((await message(pub,'viewer_joined'))['peer'],bid)
        for viewer,vid in ((a,aid),(b,bid)):
            await pub.send_json({'type':'offer','peer':vid,'sdp':OFFER})
            require_equal(await message(viewer,'offer'),{'type':'offer','peer':pid,'sdp':OFFER})
            await viewer.send_json({'type':'answer','peer':pid,'sdp':ANSWER})
            require_equal(await message(pub,'answer'),{'type':'answer','peer':vid,'sdp':ANSWER})
        require_equal(w.ledger.recent_runs(),[])
        require_equal(await w.memory.semantic.memory.active_records(),[])
        await pub.close();await closed(a);await closed(b)


async def t_capacity_is_reserved_before_await_and_readers_cannot_replace_sender():
    async with world() as w:
        publishers=await asyncio.gather(*(connect(w,'publisher') for _ in range(2)))
        async def ready_or_closed(ws):
            row=await ws.receive(timeout=3)
            return json.loads(row.data)['type'] if row.type==WSMsgType.TEXT else 'closed'
        require_equal(sorted(await asyncio.gather(*(ready_or_closed(s) for s in publishers))),['closed','ready'])
        viewers=await asyncio.gather(*(connect(w,'viewer') for _ in range(3)))
        require_equal(sorted(await asyncio.gather(*(ready_or_closed(s) for s in viewers))),['closed','ready','ready'])
        require_equal(len(w.app[W.HUB].peers),3)


async def t_cross_channel_audio_reverse_direction_and_replayed_sdp_are_rejected():
    for bad in ('foreign_channel','audio','conflicting_direction','reverse','replay'):
        async with world() as w:
            pub,pid=await peer(w,'publisher')
            viewer,vid=await peer(w,'viewer','hermes' if bad=='foreign_channel' else 'obsidian')
            if bad!='foreign_channel':await message(pub,'viewer_joined')
            data={'type':'offer','peer':vid,'sdp':OFFER}
            if bad=='audio':data['sdp']+='m=audio 9 RTP/AVP 0\r\n'
            if bad=='conflicting_direction':data['sdp']='a=sendonly\r\n'+OFFER.replace('sendonly','sendrecv')
            if bad=='reverse':
                await viewer.send_json({'type':'offer','peer':pid,'sdp':OFFER})
                await closed(viewer);require(not pub.closed);continue
            await pub.send_json(data)
            if bad=='replay':
                await message(viewer,'offer');await pub.send_json(data)
            await closed(pub)



def browser_sdp(session_direction=None, media_direction=None, *, newline='\r\n'):
    # Synthetic browser-shaped SDP only: no captured identifiers, candidates or keys.
    lines=['v=0','o=- 1 1 IN IP4 127.0.0.1','s=-','t=0 0','a=group:BUNDLE 0']
    if session_direction is not None:lines.append('a='+session_direction)
    lines.extend(['m=video 9 UDP/TLS/RTP/SAVPF 96','c=IN IP4 0.0.0.0','a=mid:0'])
    if media_direction is not None:lines.append('a='+media_direction)
    lines.extend(['a=rtcp-mux','a=rtpmap:96 VP8/90000'])
    return newline.join(lines)+newline


async def rejected_signal(w, kind, description):
    pub,pid=await peer(w,'publisher','hermes');viewer,vid=await peer(w,'viewer','hermes')
    await message(pub,'viewer_joined')
    if kind=='answer':
        await pub.send_json({'type':'offer','peer':vid,'sdp':browser_sdp(media_direction='sendonly')})
        await message(viewer,'offer')
    source,target=(pub,vid) if kind=='offer' else (viewer,pid)
    await source.send_json({'type':kind,'peer':target,'sdp':description})
    await closed(source)
    if kind=='answer':require(not pub.closed,'Rejected answer must not close its publisher')
    require_equal(w.ledger.recent_runs(),[])


async def t_media_direction_overrides_session_default_through_public_socket():
    # RFC8866 section6.7: Firefox's session sendrecv + video recvonly is valid.
    for session in ('sendrecv','same','opposite'):
        offer_session={'same':'sendonly','opposite':'recvonly'}.get(session,session)
        answer_session={'same':'recvonly','opposite':'sendonly'}.get(session,session)
        offer=browser_sdp(offer_session,'sendonly');answer=browser_sdp(answer_session,'recvonly')
        async with world() as w:
            pub,pid=await peer(w,'publisher','hermes');viewer,vid=await peer(w,'viewer','hermes')
            await message(pub,'viewer_joined')
            await pub.send_json({'type':'offer','peer':vid,'sdp':offer})
            received=await message(viewer,'offer');require(received['sdp']==offer,'Offer bytes must stay unchanged');require_equal(received['peer'],pid)
            await viewer.send_json({'type':'answer','peer':pid,'sdp':answer})
            received=await message(pub,'answer');require(received['sdp']==answer,'Answer bytes must stay unchanged');require_equal(received['peer'],vid)
            require_equal(w.ledger.recent_runs(),[]);require_equal(await w.memory.semantic.memory.active_records(),[])
            await pub.close();await closed(viewer)


async def t_single_direction_can_be_inherited_or_media_local_without_normalizing_sdp():
    for level in ('session','media'):
        for newline in ('\r\n','\n'):
            offer=browser_sdp('sendonly' if level=='session' else None,'sendonly' if level=='media' else None,newline=newline)
            answer=browser_sdp('recvonly' if level=='session' else None,'recvonly' if level=='media' else None,newline=newline)
            async with world() as w:
                pub,pid=await peer(w,'publisher');viewer,vid=await peer(w,'viewer');await message(pub,'viewer_joined')
                await pub.send_json({'type':'offer','peer':vid,'sdp':offer});require((await message(viewer,'offer'))['sdp']==offer,'Inherited offer changed')
                await viewer.send_json({'type':'answer','peer':pid,'sdp':answer});require((await message(pub,'answer'))['sdp']==answer,'Inherited answer changed')
                await pub.close();await closed(viewer)


async def t_duplicate_direction_in_either_scope_is_rejected_even_if_equal():
    for kind,direction in (('offer','sendonly'),('answer','recvonly')):
        for level in ('session','media'):
            for repeated in (direction,'sendrecv'):
                if level=='session':
                    description=browser_sdp(direction,direction).replace('m=video','a='+repeated+'\r\nm=video',1)
                else:description=browser_sdp('sendrecv',direction)+'a='+repeated+'\r\n'
                async with world() as w:await rejected_signal(w,kind,description)


async def t_effective_direction_never_gains_reverse_audio_or_control_authority():
    for kind,direction in (('offer','sendonly'),('answer','recvonly')):
        opposite='recvonly' if direction=='sendonly' else 'sendonly'
        for description in (browser_sdp(direction,'sendrecv'),browser_sdp(direction,opposite),
                            browser_sdp(direction,'inactive'),browser_sdp('sendrecv'),browser_sdp()):
            async with world() as w:await rejected_signal(w,kind,description)


async def t_sdp_section_ambiguity_and_additional_media_are_rejected_publicly():
    answer=browser_sdp(media_direction='recvonly')
    cases=[
        answer+'m=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=recvonly\r\n',
        answer+'m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n',
        answer+'m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\n',
        answer.replace('m=video',' m=video'),
        answer.replace('a=recvonly','a=recvonly\r\nv=0'),
        answer.replace('a=recvonly','a=recvonly\r\no=- 2 2 IN IP4 127.0.0.1'),
        answer.replace('a=recvonly','a=recvonly\r\na=sendonly:extra'),
        answer.replace('a=recvonly','a=recvonly\r\na=sendrecv '),
        answer.replace('a=recvonly','a=recvonly\r\na=sendrecv extra'),
        answer.replace('a=recvonly','a=recvonly\ra=sendrecv'),
        answer.replace('a=recvonly','a=recvonly\va=sendrecv'),
        answer.replace('a=recvonly','a=recvonly\r\n\r\na=rtcp-mux'),
    ]
    for description in cases:
        async with world() as w:await rejected_signal(w,'answer',description)


async def t_revocation_expiry_and_authentication_failure_close_existing_connections():
    for mode in ('revoked','expired','unavailable'):
        async with world() as w:
            with patch.object(W,'LEASE_INTERVAL',.02):
                pub,_=await peer(w,'publisher');viewer,_=await peer(w,'viewer')
                session=await (await w.client.get('/v1/browser/session')).json()
                if mode=='revoked':
                    await w.sessions.revoke(session['session_id'],principal='local-owner')
                    await closed(pub);await closed(viewer)
                elif mode=='expired':
                    with patch.object(w.sessions,'_clock',return_value=session['expires_at']+1):
                        await closed(pub);await closed(viewer)
                else:
                    with patch.object(w.sessions,'authenticate',side_effect=RuntimeError('local store unavailable')):
                        await closed(pub);await closed(viewer)


async def t_stale_recipient_does_not_receive_new_sender_generation():
    async with world() as w:
        pub,_=await peer(w,'publisher');viewer,vid=await peer(w,'viewer')
        await message(pub,'viewer_joined');await viewer.close();await message(pub,'viewer_left')
        fresh,fid=await peer(w,'viewer');await message(pub,'viewer_joined')
        require(fid!=vid)
        await pub.send_json({'type':'offer','peer':vid,'sdp':OFFER})
        await closed(pub);await closed(fresh)


async def t_failed_recipient_is_removed_without_closing_other_viewer_or_sender():
    async with world() as w:
        pub,_=await peer(w,'publisher');a,aid=await peer(w,'viewer');await message(pub,'viewer_joined')
        b,bid=await peer(w,'viewer');await message(pub,'viewer_joined')
        target=w.app[W.HUB].peers[aid]
        with patch.object(target.ws,'send_json',side_effect=ConnectionResetError('synthetic recipient failure')):
            await pub.send_json({'type':'offer','peer':aid,'sdp':OFFER})
            require_equal((await message(pub,'viewer_left'))['peer'],aid)
        await closed(a)
        await pub.send_json({'type':'offer','peer':bid,'sdp':OFFER})
        require_equal((await message(b,'offer'))['sdp'],OFFER)
        require(not pub.closed)


async def t_shutdown_refuses_a_handshake_that_was_waiting_on_authentication():
    async with world() as w:
        entered=asyncio.Event();release=asyncio.Event();original=W.owner_browser
        async def held(request):
            actor=await original(request);entered.set();await release.wait();return actor
        with patch.object(W,'owner_browser',held):
            pending=asyncio.create_task(w.client.ws_connect(PATH,headers={'Origin':w.origin}))
            try:
                await asyncio.wait_for(entered.wait(),3)
                await w.app.shutdown();release.set()
                try:await pending
                except WSServerHandshakeError as exc:require_equal(exc.status,503)
                else:raise AssertionError('joined after shutdown')
                require_equal(w.app[W.HUB].peers,{})
                require_equal(w.app[W.HUB].sockets,set())
            finally:
                release.set();await asyncio.gather(pending,return_exceptions=True)


async def t_native_renderer_assets_are_pinned_private_and_cannot_read_other_files():
    from solvio.dashboard.hermes_view import ROOT
    import hashlib
    manifest=json.loads((ROOT/'BUILD.json').read_text())
    async with world() as w:
        for name,expected in manifest['files'].items():
            require_equal(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),expected)
        page=await w.client.get('/dashboard/hermes/solvio-view.html')
        require_equal(page.status,200)
        require("frame-ancestors 'self'" in page.headers['Content-Security-Policy'])
        require_equal(page.headers['X-Frame-Options'],'SAMEORIGIN')
        for name in ('BUILD.json','../app.js','assets/no-file.js'):
            require_equal((await w.client.get('/dashboard/hermes/'+name)).status,404)
        require_equal(w.ledger.recent_runs(),[])


async def t_disconnecting_http_handler_cannot_interrupt_peer_removal_notification():
    async with world() as w:
        pub,_=await peer(w,'publisher');viewer,vid=await peer(w,'viewer')
        await message(pub,'viewer_joined')
        entered=asyncio.Event();release=asyncio.Event();original=W.Hub.send
        async def held(hub,recipient,data):
            if data['type']=='viewer_left':
                entered.set();await release.wait()
            await original(hub,recipient,data)
        with patch.object(W.Hub,'send',held):
            closing=asyncio.create_task(viewer.close())
            try:
                await asyncio.wait_for(entered.wait(),3)
                await asyncio.wait_for(closing,3)  # connection_lost cancels its handler
                require(vid not in w.app[W.HUB].peers)
                require(w.app[W.HUB].cleanups, 'cleanup was not retained independently')
                release.set()
                require_equal((await message(pub,'viewer_left'))['peer'],vid)
                fresh,_=await peer(w,'viewer');await message(pub,'viewer_joined')
                await pub.close();await closed(fresh)
            finally:
                release.set();await asyncio.gather(closing,return_exceptions=True)


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))

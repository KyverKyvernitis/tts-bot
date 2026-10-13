"""Production command/edit methods with Discord transport doubles, no SDK claim."""
from pathlib import Path
import asyncio
import ast
import logging
from types import SimpleNamespace, ModuleType
import sys
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
import weakref

ROOT=Path(__file__).resolve().parents[1]


def extract(path,names,namespace):
    tree=ast.parse((ROOT/path).read_text())
    nodes=[node for node in ast.walk(tree) if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.returns=None
        for arg in (*node.args.args,*node.args.kwonlyargs): arg.annotation=None
    exec(compile(ast.Module(body=nodes,type_ignores=[]),path,'exec'),namespace)
    return {name:namespace[name] for name in names}


class HTTPError(Exception):
    status=500
    code=0


DISCORD=SimpleNamespace(HTTPException=HTTPError,AllowedMentions=SimpleNamespace(none=lambda:None))


class View:
    generation=0
    def __init__(self,cog,owner_id,guild_id,timeout=300):
        self.cog,self.owner_id,self.guild_id=cog,owner_id,guild_id
        self._activity_timeout=timeout
        self.expires_at_monotonic=time.monotonic()+timeout
        self.message=None
        self.finished=False
        self.refresh=AsyncMock()
        self._edit_panel=AsyncMock(return_value=True)
        self.panel_files=Mock(return_value=[])
        self.remember_cover=Mock()
        self.stop=Mock(side_effect=self.finish)
    def finish(self): self.finished=True
    def _is_expired(self): return self.expires_at_monotonic<=time.monotonic()
    def is_finished(self): return self.finished
    @property
    def emoji_generation(self): return type(self).generation


class PanelReuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        namespace={'__package__':'cogs.tts','asyncio':asyncio,'time':time,'weakref':weakref,
                   'discord':DISCORD,'logger':logging.getLogger('test-cd-runtime')}
        methods=extract('cogs/tts/cog.py',['_send_cd_panel','_delete_prefix_panel','_prefix_panel_key'],namespace)
        self.cog=type('ProductionCommand',(),methods)()
        self.cog._get_db=lambda:object()
        self.cog._check_prefix_panel_cooldown=AsyncMock(return_value=False)
        self.cog._get_panel_prefix_hint=AsyncMock(return_value='`_cd`')
        self.cog._active_prefix_panels={}
        self.cog._public_panel_states={}
        self.cog._prefix_panel_cooldowns={}
        View.generation=0
        self.message=SimpleNamespace(guild=SimpleNamespace(id=20),author=SimpleNamespace(id=10),channel=None)
        self.channel=SimpleNamespace(id=30,send=AsyncMock(side_effect=self.send))
        self.message.channel=self.channel
        self.sent=[]
        module=ModuleType('cogs.tts.interface.painel_canto')
        module.CDPanelView=View
        self.modules=patch.dict(sys.modules,{'cogs.tts.interface.painel_canto':module})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    async def send(self,**kwargs):
        result=SimpleNamespace(id=100+len(self.sent),channel=self.channel,delete=AsyncMock(),view=kwargs['view'])
        self.sent.append(result)
        return result

    async def test_reopen_reuses_one_active_panel_and_never_deletes_it(self):
        await self.cog._send_cd_panel(self.message)
        key=(20,10,'cd')
        view=self.cog._cd_panel_views[key]
        await self.cog._send_cd_panel(self.message)
        self.assertEqual(self.channel.send.await_count,1)
        self.sent[0].delete.assert_not_called()
        self.assertIs(self.cog._cd_panel_views[key],view)
        self.assertEqual(view._edit_panel.await_count,1)
        self.assertGreater(view.expires_at_monotonic,time.monotonic())

    async def test_expired_panel_is_replaced_and_not_reactivated(self):
        await self.cog._send_cd_panel(self.message)
        old=self.cog._cd_panel_views[(20,10,'cd')]
        old.expires_at_monotonic=0
        await self.cog._send_cd_panel(self.message)
        self.assertEqual(self.channel.send.await_count,2)
        self.sent[0].delete.assert_awaited_once()
        old.stop.assert_called_once()
        self.assertEqual(old.expires_at_monotonic,0)
        self.assertIsNot(self.cog._cd_panel_views[(20,10,'cd')],old)

    async def test_emoji_ready_during_message_upload_is_applied_after_attachment(self):
        async def slow_send(**kwargs):
            View.generation+=1
            return await self.send(**kwargs)
        self.channel.send.side_effect=slow_send
        await self.cog._send_cd_panel(self.message)
        self.sent[0].view._edit_panel.assert_awaited_once()

    async def test_panel_expiring_during_settings_refresh_is_replaced(self):
        await self.cog._send_cd_panel(self.message)
        old = self.cog._cd_panel_views[(20,10,'cd')]
        async def expires(**kwargs):
            old.expires_at_monotonic = 0
        old.refresh.side_effect = expires
        await self.cog._send_cd_panel(self.message)
        self.assertEqual(self.channel.send.await_count, 2)
        old._edit_panel.assert_not_called()
        old.stop.assert_called_once()
        self.assertEqual(old.expires_at_monotonic, 0)

    async def test_other_user_gets_separate_panel(self):
        await self.cog._send_cd_panel(self.message)
        other=SimpleNamespace(guild=self.message.guild,author=SimpleNamespace(id=11),channel=self.channel)
        await self.cog._send_cd_panel(other)
        self.assertEqual(self.channel.send.await_count,2)
        self.sent[0].delete.assert_not_called()
        self.assertIn((20,10,'cd'),self.cog._cd_panel_views)
        self.assertIn((20,11,'cd'),self.cog._cd_panel_views)

    async def test_unchanged_cover_uses_existing_attachment_without_new_upload(self):
        revision=['abc']
        namespace={'discord':DISCORD,'cover_revision':lambda _:revision[0]}
        namespace['asyncio'] = asyncio
        methods=extract('cogs/tts/interface/painel_canto.py',['_edit_panel','_edit_panel_current'],namespace)
        view=type('ProductionEdit',(),methods)()
        attachment=SimpleNamespace(filename='ttv-cd-rolling-girl.jpg')
        message=SimpleNamespace(attachments=[attachment],edit=AsyncMock())
        message.edit.return_value=message
        view.message=message
        view.selected_melody_id='rolling-girl'
        view._cover_attachment_key=('rolling-girl','abc')
        view._rebuild=Mock()
        view.panel_files=Mock(side_effect=AssertionError('same attachment must not re-upload'))
        self.assertTrue(await view._edit_panel())
        self.assertEqual(message.edit.call_args.kwargs['attachments'],[attachment])
        revision[0]='new-cover'
        upload=SimpleNamespace(close=Mock())
        view.panel_files=Mock(return_value=[upload])
        self.assertTrue(await view._edit_panel())
        view.panel_files.assert_called_once()
        upload.close.assert_called_once()
        self.assertEqual(message.edit.call_args.kwargs['attachments'],[upload])


if __name__=='__main__': unittest.main()

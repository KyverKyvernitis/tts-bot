"""Execute exact production methods with collaborators; no fake SDK claim."""
from pathlib import Path
import ast
import asyncio
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import time

ROOT = Path(__file__).resolve().parents[1]


def methods(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text())
    nodes = [node for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.returns = None
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            argument.annotation = None
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return {name: namespace[name] for name in names}


async def maybe(value): return await value if inspect.isawaitable(value) else value


class Parent:
    def _is_expired(self): return self.expires_at_monotonic <= time.monotonic()
    def is_finished(self): return False


class PanelOptimizationTests(unittest.IsolatedAsyncioTestCase):
    def panel(self, prepared=None):
        namespace = {'time':time, 'asyncio':asyncio, '_maybe_await':maybe,
                     'prepared_sample':Mock(return_value=prepared),
                     'resolve_preferences':lambda values: {'ttv_voice_id':'kasane-teto','ttv_language':'pt-BR',**values},
                     'get_melody':lambda value: SimpleNamespace(id=value),
                     'DEFAULT_MELODY_ID':'default', 'start_catalog_watch':Mock(),
                     '_CD_EMOJIS':SimpleNamespace(schedule=Mock())}
        extracted = methods('cogs/tts/interface/painel_canto.py',
                            ['refresh','_refresh_current','_refresh_availability'], namespace)
        cls = type('ProductionMethods', (Parent,), extracted)
        view = cls()
        db = SimpleNamespace(resolve_tts=AsyncMock(return_value={}))
        view.cog = SimpleNamespace(_get_db=lambda:db, bot=None,
            _ttv_voice_status=Mock(return_value={'online':True,'ready':True,'available':True}),
            _tts_phone_worker_online_for_ui=Mock(return_value=True))
        view._settings_lock=asyncio.Lock()
        view._settings_at=0
        view._preferences={'ttv_voice_id':'kasane-teto','ttv_language':'pt-BR'}
        view._settings_available=False
        view.guild_id=20
        view.owner_id=10
        view._initialized=False
        view._availability_note=''
        view.active_melody_id=view.selected_melody_id='default'
        view.expires_at_monotonic=time.monotonic()+100
        view._rebuild=Mock()
        return view, db, namespace

    async def test_prepared_sample_avoids_worker_queries_and_reuses_settings(self):
        view, db, _ = self.panel(prepared=b'validated audio')
        deadline = view.expires_at_monotonic
        await view.refresh()
        await view.refresh(rebuild=False)
        self.assertTrue(view._preview_available)
        self.assertEqual(db.resolve_tts.await_count, 1)
        view.cog._ttv_voice_status.assert_not_called()
        view.cog._tts_phone_worker_online_for_ui.assert_not_called()
        self.assertEqual(view._rebuild.call_count, 1)
        self.assertEqual(view.expires_at_monotonic, deadline)
        await view.refresh(force=True)
        self.assertEqual(db.resolve_tts.await_count, 2)

    async def test_live_status_reads_once_without_duplicate_online_query_or_stale_cache(self):
        view, db, _ = self.panel()
        await view.refresh()
        await view.refresh()
        self.assertEqual(view.cog._ttv_voice_status.call_count, 2)
        view.cog._tts_phone_worker_online_for_ui.assert_not_called()
        self.assertEqual(db.resolve_tts.await_count, 1)
        view.cog._ttv_voice_status.return_value['online']=False
        await view._refresh_availability()
        self.assertEqual(view.cog._ttv_voice_status.call_count, 3)
        self.assertFalse(view._preview_available)

    async def test_cd_uses_exact_tts_expiration_with_only_command_changed(self):
        namespace={'time':time, 'config':SimpleNamespace(BOT_PREFIX='_')}
        shared=methods('cogs/tts/interface/visoes_base.py',
            ['dica_comando_painel_expirado','mensagem_painel_expirado','responder_painel_tts_de_outro'],namespace)
        namespace['EMOJI_PAINEL_TTS_EXPIRADO']='<:osaka:1539137127852539944>'
        namespace['MENSAGEM_PAINEL_TTS_DE_OUTRO']='Esse painel neh seu não djacho!'
        parent_methods=methods('cogs/tts/interface/visoes_layout.py',['interaction_check'],namespace)
        base=type('SharedProductionBase',(Parent,),parent_methods)
        replies=[]
        async def reply(interaction,content,**kwargs): replies.append(content)
        namespace.update(_reply=reply, _maybe_await=maybe,
                         VisaoLayoutBaseTTS=base)
        # super() needs the class closure; compile the exact method inside its class.
        node=next(n for n in ast.walk(ast.parse((ROOT/'cogs/tts/interface/painel_canto.py').read_text()))
                  if isinstance(n,ast.AsyncFunctionDef) and n.name=='interaction_check')
        node.returns=None
        for argument in node.args.args: argument.annotation=None
        clsnode=ast.ClassDef(name='CDCheck',bases=[ast.Name(id='VisaoLayoutBaseTTS',ctx=ast.Load())],
                            keywords=[],body=[node],decorator_list=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[clsnode],type_ignores=[])),'CDCheck','exec'),namespace)
        view=namespace['CDCheck']()
        view.expires_at_monotonic=0
        view.guild_id=20
        view.owner_id=10
        view.panel_kind='cd'
        view.cog=SimpleNamespace(_build_expired_panel_message=AsyncMock(side_effect=RuntimeError()))
        interaction=SimpleNamespace(guild=SimpleNamespace(id=20),user=SimpleNamespace(id=10),
            response=SimpleNamespace(is_done=lambda:False,send_message=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        self.assertFalse(await view.interaction_check(interaction))
        call=interaction.response.send_message.call_args
        self.assertTrue(call.kwargs['ephemeral'])
        self.assertEqual(call.args[0], shared['mensagem_painel_expirado']('user').replace('`_tts`','`_cd`'))
        self.assertEqual(view.expires_at_monotonic,0)


if __name__=='__main__': unittest.main()

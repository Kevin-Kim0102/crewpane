import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import watch_kiro as watch
import agent_roles


class ReliabilityPanelTests(unittest.TestCase):
    def test_role_identity_is_visible_in_grouped_conversation(self):
        grouped = watch.group_agents([{'id': 'r1', 'agent_id': 'a', 'role_id': 'tester',
                                      'role_name': '测试', 'status': 'completed'}])
        self.assertEqual(grouped[0].get('role_id'), 'tester')
        self.assertEqual(grouped[0].get('role_name'), '测试')

    def test_source_calls_shared_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'runs'; directory = root / 'run1'; directory.mkdir(parents=True)
            (directory / 'state.json').write_text(json.dumps({'status': 'running', 'agent_id': 'a'}))
            source = watch.Source(tmp, root)
            with mock.patch('agent_tasks.recover', return_value={'id': 'run1', 'agent_id': 'a', 'status': 'cancelled'}) as recover:
                self.assertEqual(source.list_runs()[0]['status'], 'cancelled')
                recover.assert_called_once()

    def test_panel_permission_selection_is_bounded(self):
        self.assertEqual(watch.permission_selection({'permission_mode': 'acceptEdits'}, 'claude'), 'acceptEdits')
        with self.assertRaises(ValueError):
            watch.permission_selection({'permission_mode': 'bypassPermissions'}, 'claude')
        with self.assertRaises(ValueError):
            watch.permission_selection({'permission_mode': 'acceptEdits'}, 'kiro')

    def test_role_continuation_uses_dispatcher_and_current_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = watch.Source(tmp, Path(tmp)/'runs')
            state = {'id':'old','agent_id':'a','provider':'claude','status':'completed',
                     'session_id':'12345678-1234-4234-8234-123456789abc','cwd':tmp,'role_id':'coder'}
            role = {**next(r for r in agent_roles.DEFAULTS if r["id"] == "coder"), 'model':'new-model','effort':'high'}
            def fake_spawn(command, **kwargs):
                directory=Path(command[command.index('--run-dir')+1]);directory.mkdir()
                (directory/'state.json').write_text('{}')
                identity = agent_roles.identity(kwargs['env'])
                self.assertEqual(identity['role_id'], 'coder')
                override = '--resume-id' not in command
                self.assertEqual(identity.get('role_execution_override', False), override)
                self.assertEqual(command[command.index('--model')+1], 'manual' if override else 'new-model')
                self.assertEqual(command[command.index('--effort')+1],'high')
                self.assertIn('# 角色',kwargs['stdin'].read())
                return mock.Mock(pid=999,wait=mock.Mock(return_value=0))
            with mock.patch.object(source,'list_runs',return_value=[state]),mock.patch.object(agent_roles,'continuation',return_value=(role,agent_roles.task_prompt(role,'follow up'))),mock.patch('subprocess.Popen',side_effect=fake_spawn):
                source.start_turn('follow up',agent_id='a',provider='claude',cli_path='/fake/claude')
                source.start_turn('new task', cwd=tmp, provider='kiro', cli_path='/fake/kiro',
                                  role=role, role_override=True, selection={'model': 'manual', 'effort': 'high'})

    def test_panel_role_uses_defaults_or_keeps_explicit_execution_for_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            panel = watch.Panel.__new__(watch.Panel)
            panel.fixture = None
            panel.injected_services = False
            panel.config_path = Path(tmp) / 'config.json'
            panel.roles = mock.Mock()
            role = {**next(r for r in agent_roles.DEFAULTS if r['id'] == 'coder'),
                    'enabled': True, 'provider': 'claude', 'model': 'role-model', 'effort': 'high'}
            panel.roles.get.return_value = role
            panel.connections = mock.Mock()
            panel.catalog = mock.Mock()
            panel.catalog.refresh.return_value.projects = []
            panel.resolve_selection = mock.Mock(side_effect=lambda data, *_args, **_kwargs:
                                                {'model': data['model'], 'effort': data['effort']})
            source = mock.MagicMock()
            source.start_turn.return_value = (201, {})
            panel.new_target = mock.Mock(return_value=(source, tmp))
            panel.retain = mock.Mock()
            panel.confirmed = mock.Mock()
            base = {'text': 'implement', 'group': 'project', 'role_id': 'coder'}
            panel.send(base)
            self.assertEqual(source.start_turn.call_args.kwargs['provider'], 'claude')
            self.assertEqual(source.start_turn.call_args.kwargs['selection'], {'model': 'role-model', 'effort': 'high'})
            self.assertFalse(source.start_turn.call_args.kwargs['role_override'])
            panel.send({**base, 'provider': 'kiro', 'model': 'manual', 'effort': None})
            self.assertEqual(source.start_turn.call_args.kwargs['provider'], 'kiro')
            self.assertEqual(source.start_turn.call_args.kwargs['selection'], {'model': 'manual', 'effort': None})
            self.assertTrue(source.start_turn.call_args.kwargs['role_override'])
            self.assertEqual(source.start_turn.call_args.kwargs['role']['id'], 'coder')
            panel.source_by_id = mock.Mock(return_value=source)
            panel.group_of = mock.Mock(return_value='project')
            source.list_runs.return_value = [{'id': 'r', 'agent_id': 'a', 'provider': 'kiro', 'role_id': 'coder',
                                              'role_execution_override': True}]
            source.recorded_selection.return_value = ('manual', None)
            panel.send({'text': 'continue', 'group': 'project', 'source': 's', 'agent': 'a'})
            self.assertEqual(source.start_turn.call_args.kwargs['provider'], 'kiro')
            self.assertEqual(source.start_turn.call_args.kwargs['selection'], {'model': 'manual', 'effort': None})
            self.assertTrue(source.start_turn.call_args.kwargs['role_override'])

    def test_task_stop_requires_matching_group(self):
        panel=watch.Panel.__new__(watch.Panel)
        panel.catalog=mock.Mock();panel.source_by_id=mock.Mock(return_value=mock.Mock())
        panel.group_of=mock.Mock(return_value='correct')
        with mock.patch('agent_tasks.stop') as stop, self.assertRaises(ValueError):
            panel.task_stop({'source':'s','group':'wrong','run':'r'})
        stop.assert_not_called()

    def test_saved_default_is_checked_against_known_project_directory(self):
        from agent_providers import ClaudeModels
        from kiro_catalog import ConfigError
        panel=watch.Panel.__new__(watch.Panel);panel.injected_services=False
        panel.connections=mock.Mock();panel.connections.defaults.return_value={'model':'missing','effort':'high'}
        panel.claude_models=ClaudeModels()
        with mock.patch.object(panel.claude_models,'snapshot',return_value={'status':'ok','models':[{'id':'present'}]}),self.assertRaises(ConfigError):
            panel.resolve_selection({},None,'claude',cwd='/project')

    def test_recorded_alias_can_continue_without_claiming_catalog_membership(self):
        from agent_providers import ClaudeModels
        panel=watch.Panel.__new__(watch.Panel);panel.injected_services=False
        panel.connections=mock.Mock();panel.claude_models=ClaudeModels()
        with mock.patch.object(panel.claude_models,'snapshot',return_value={'status':'ok','models':[{'id':'claude-sonnet-full'}]}):
            self.assertEqual(panel.resolve_selection({'model':'sonnet','effort':'medium'},('sonnet','medium'),'claude'),{'model':'sonnet','effort':'medium'})

    def test_diagnostic_redacts_credentials_loaded_from_cli_settings(self):
        with mock.patch('claude_catalog.resolve_config', return_value={'headers':{'Authorization':'Bearer fake-private-value'}}):
            result=watch.safe_diagnostics('API returned fake-private-value',{'provider':'claude','cwd':'/project'})
        self.assertNotIn('fake-private-value',result['detail'])

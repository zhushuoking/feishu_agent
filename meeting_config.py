"""唯一会议目标配置；未完成官方接口核验时禁止真实访问。"""
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / 'config/meeting_target.json'
NEW_TABLE = 'example_fixture2'
WIKI_NODE = 'EXAMPLE_RESOURCE_2'


def target(require_ready=False):
    config = json.loads(CONFIG_PATH.read_text())
    if config.get('table_id') != NEW_TABLE or config.get('wiki_node') != WIKI_NODE:
        raise ValueError('会议目标必须为当前正式账号新表')
    app = config.get('app_token', '')
    if app == WIKI_NODE:
        raise ValueError('wiki节点不是app_token')
    if require_ready and (not app or not config.get('schema_verified')):
        raise ValueError('新表app_token及API字段权限尚未核验，会议功能保持关闭')
    return config

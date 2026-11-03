from pathlib import Path
import json, re, shutil, hashlib, xml.etree.ElementTree as E, datetime
src=Path(r'D:\Photos\build\final-validation-e9805a7-20261103')
repo=Path(r'C:\Users\alice\.codex\worktrees\final-validation-e9805a7\远程照片')
dst=Path(r'D:\Photos\report\验收证据-e9805a7-2026-11-03')
dst.mkdir(parents=True,exist_ok=True)
redactions=0
def redact(s):
 global redactions
 patterns=[(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+',r'\1[REDACTED]'),(r'(?i)([?&#](?:token|ticket|code)=)[^\s\'\"&#<>\]]+',r'\1[REDACTED]'),(r'(?i)("(?:client_token|account_token|agent_token|access_token|refresh_token|pb\.token)"\s*:\s*")[^"\r\n]+',r'\1[REDACTED]'),(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])','[REDACTED-TEMP-TOKEN]')]
 for pat, repl in patterns:
  s,n=re.subn(pat,repl,s);redactions+=n
 lines=[]
 for line in s.splitlines(keepends=True):
  if any(x in line for x in ['配对码','code=','code%3D','配对后 token','token 落盘']):
   line,n=re.subn(r'(?<!\d)\d{6}(?!\d)','[REDACTED-CODE]',line);redactions+=n
  lines.append(line)
 return ''.join(lines)
for f in sorted(src.iterdir()):
 if f.suffix in ['.log','.json','.py']:
  (dst/f.name).write_text(redact(f.read_text(encoding='utf-8-sig',errors='replace')),encoding='utf-8')
for name in ['settings-qr-confirmed.png','fresh-wrong-device.png','cloud-viewer-original.png','cloud-viewer-next.png','lan-viewer-original.png']:
 if (src/name).exists():shutil.copy2(src/name,dst/name)
for f,name in [(repo/'build/final-pytest-ready.xml','pytest-final.xml'),(repo/'build/final-pytest.xml','pytest-initial.xml'),(repo/'build/final-lan-soak-report.json','lan-soak.json'),(repo/'build/final-tunnel-report.json','tunnel-relative-path.json'),(repo/'build/final-tunnel-absolute-report.json','tunnel-absolute-path.json'),(repo/'dist/PhotoBridge/build_info.json','packaged-build-info.json')]:
 (dst/name).write_text(redact(f.read_text(encoding='utf-8',errors='replace')),encoding='utf-8')
exe=repo/'dist/PhotoBridge/PhotoBridge.exe'
(dst/'packaged-exe-sha256.json').write_text(json.dumps({'path':str(exe),'bytes':exe.stat().st_size,'sha256':hashlib.sha256(exe.read_bytes()).hexdigest()},ensure_ascii=False,indent=2),encoding='utf-8')
rows=[]
for p in sorted(src.glob('*.result.json')):
 row=json.loads(p.read_text(encoding='utf-8'));rows.append(row)
(dst/'runs-summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
summary=json.loads((src/'pytest-summary.json').read_text(encoding='utf-8'))
xml=E.parse(repo/'build/final-pytest-ready.xml');files={}
for c in xml.findall('.//testcase'):
 classname=c.get('classname','');match=re.match(r'backend\.tests\.(test_[^.]+)',classname);name=match.group(1) if match else classname
 counts=files.setdefault(name,{'passed':0,'skipped':0,'failed':0})
 key='skipped' if c.find('skipped') is not None else ('failed' if c.find('failure') is not None or c.find('error') is not None else 'passed');counts[key]+=1
(dst/'pytest-files.json').write_text(json.dumps(files,ensure_ascii=False,indent=2),encoding='utf-8')
readme=f'''# 本轮测试证据说明

基线：e9805a76f3fe29766a2d7876fd1a99bf13106326。所有结果来自本轮实际执行。

- `pytest-final.xml` 为环境就绪后的最终全量结果；`pytest-initial.xml` 保留构建未完成时的首轮失败。
- `*.result.json` 的 exit_code 是被测进程退出码；不等同于所有产品行为通过。例如 `e2e-lan-viewer-evidence` 仅断言失败可复现，产品结果见 `lan-viewer-evidence.json` 的 passed=false。
- `e2e-browser`、`e2e-browser-fresh`、`e2e-browser-functional-subset` 均非全通过。补充导航与显式排除 4n 只用于缩小测试工具问题，不修改产品、不把未测项算通过。
- `focused-qr` 的退出码0表示证据收集和前置断言成功；二维码自身 decoded=false，属于产品失败。
- 辅助 `*.py` 位于原 build 目录，记录了隔离与补充步骤；需把其 repo/output 参数换成复测环境，勿直接重跑注册表/固定目录清理类旧脚本。
- 只保存日志、结果、截图、辅助脚本、构建元数据；没有复制账号数据库、secrets.json、真实照片或测试媒体。
- 日志中的临时令牌、配对码与查询票据已作规则脱敏（替换 {redactions} 处）。原始证据仍在本机 build/final-validation-e9805a7-20261103，勿将其直接作为公开附件。
- `manifest.json` 给出各附件 SHA-256，可检查报告交付后是否发生变动。
- XML 按文件汇总共 {len(files)} 个测试文件；统计与收集项见 `pytest-files.json`、`pytest-summary.json`。
'''
(dst/'README.md').write_text(readme,encoding='utf-8')
print(json.dumps({'destination':str(dst),'files':len(list(dst.iterdir())),'test_files':len(files),'redactions':redactions},ensure_ascii=False))

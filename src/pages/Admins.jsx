import { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { SkeletonRows, InlineSpinner, useToast } from '../lib/ui.jsx'

export default function Admins({ me }) {
  const t = useToast()
  const [rows, setRows] = useState(null)
  const [uid, setUid] = useState('')
  const [name, setName] = useState('')
  const [role, setRole] = useState('admin')
  const [health, setHealth] = useState(null)
  const [backups, setBackups] = useState([])
  const [fields, setFields] = useState([])
  const [nf, setNf] = useState({ key: '', label: '', type: 'text', options: '' })
  const [settings, setSettings] = useState({})
  const [gctx, setGctx] = useState('')
  const [regNext, setRegNext] = useState('')
  const [regCourses, setRegCourses] = useState('')
  const [accts, setAccts] = useState([])
  const [na, setNa] = useState({ course: '', bank: '', account_no: '', account_name: '', price: '', full_price: '' })
  const [busy, setBusy] = useState('')

  const load = () => api.admins().then((d) => setRows(d.admins)).catch((e) => t.err(e.message))
  const loadSys = () => {
    api.health().then(setHealth).catch(() => setHealth({ ok: false }))
    api.backupList().then((d) => setBackups(d.backups)).catch(() => {})
    api.fields().then((d) => setFields(d.fields)).catch(() => {})
    api.settings().then((d) => {
      setSettings(d.settings); setGctx(d.settings.gemini_context || '')
      setRegNext(d.settings.reg_next_url || '')
      setRegCourses(Array.isArray(d.settings.reg_courses) ? d.settings.reg_courses.join(', ') : (d.settings.reg_courses || ''))
    }).catch(() => {})
    api.payAccounts().then((d) => setAccts(d.accounts)).catch(() => {})
  }
  const addAcct = async () => {
    try {
      await api.savePayAccount({ ...na, price: +na.price || null, full_price: +na.full_price || null })
      t.ok('เพิ่มบัญชีแล้ว'); setNa({ course: '', bank: '', account_no: '', account_name: '', price: '', full_price: '' }); loadSys()
    } catch (e) { t.err(e.message) }
  }
  const toggleSetting = async (key, val) => {
    setSettings((s) => ({ ...s, [key]: val }))
    try { await api.setSetting(key, val) } catch (e) { t.err(e.message) }
  }
  const addField = async () => {
    try {
      await api.saveField({ ...nf, options: nf.options.split(',').map((s) => s.trim()).filter(Boolean) })
      t.ok('เพิ่ม field แล้ว'); setNf({ key: '', label: '', type: 'text', options: '' }); loadSys()
    } catch (e) { t.err(e.message) }
  }
  useEffect(() => { load(); loadSys() }, [])

  const add = async () => {
    if (!uid.startsWith('U')) return t.err('userId ต้องขึ้นต้น U')
    try {
      await api.addAdmin({ lineUserId: uid.trim(), name, role })
      t.ok('เพิ่มแล้ว'); setUid(''); setName(''); load()
    } catch (e) { t.err(e.message) }
  }

  const backupNow = async () => {
    setBusy('backup')
    try { const r = await api.backupNow(); t.ok(`สำรองแล้ว ${r.size_kb} KB`); loadSys() }
    catch (e) { t.err(e.message) } finally { setBusy('') }
  }

  const downloadBackup = async (b) => {
    setBusy('dl' + b.id)
    try {
      const full = await api.backupGet(b.id)
      const blob = new Blob([JSON.stringify(full.tables, null, 2)], { type: 'application/json' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob); a.download = `backup-${b.day}.json`; a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) { t.err(e.message) } finally { setBusy('') }
  }

  if (!rows) return <SkeletonRows rows={5} cols={4} />
  const canEdit = ['owner', 'admin'].includes(me.role)
  const c = health?.checks || {}

  return (
    <div>
      <h1>ผู้ดูแล & ระบบ</h1>

      <section className="card">
        <h3>สถานะระบบ {health && <span className={`chip ${health.ok ? 'ok' : 'failed'}`}>{health.ok ? 'ปกติ' : 'มีปัญหา'}</span>}</h3>
        {!health ? <SkeletonRows rows={3} cols={2} /> : (
          <table className="kv">
            <tbody>
              <tr><th>ฐานข้อมูล</th><td>{c.db === 'ok' ? '✅ เชื่อมต่อได้' : `❌ ${c.db}`}</td></tr>
              <tr><th>โควตาข้อความ</th><td>{typeof c.line_quota === 'object' ? JSON.stringify(c.line_quota) : (c.line_quota ?? '–')} {c.line_usage != null && `· ใช้ ${c.line_usage.toLocaleString()}`}</td></tr>
              <tr><th>Env</th><td>
                {Object.entries(c.env || {}).map(([k, v]) => (
                  <span key={k} className={`chip ${v ? 'ok' : 'failed'}`} style={{ marginRight: 4 }}>{k}{v ? '' : ' ✗'}</span>
                ))}
              </td></tr>
              <tr><th>Cron ล่าสุด</th><td className="xs muted">
                {(c.last_cron || []).slice(0, 3).map((x, i) => <div key={i}>{x.action} · {new Date(x.created_at).toLocaleString('th-TH')}</div>)}
                {!(c.last_cron || []).length && 'ยังไม่มี'}
              </td></tr>
            </tbody>
          </table>
        )}
        <button className="sm" onClick={loadSys} style={{ marginTop: 8 }}>เช็คใหม่</button>
      </section>

      <section className="card">
        <div className="row spread">
          <h3>สำรองข้อมูล</h3>
          <button className="sm primary" onClick={backupNow} disabled={busy === 'backup'}>
            {busy === 'backup' && <InlineSpinner />}สำรองเดี๋ยวนี้
          </button>
        </div>
        <p className="muted xs">สำรองอัตโนมัติทุกวัน (21 ตาราง) → เก็บใน DB + **สำเนาใน Supabase Storage** (แยกกัน DB ล่มก็ยังมี) · เก็บ 30 วันล่าสุด</p>
        <table>
          <thead><tr><th>วันที่</th><th>ขนาด</th><th>เมื่อ</th><th></th></tr></thead>
          <tbody>
            {backups.map((b) => (
              <tr key={b.id}>
                <td>{b.day}</td>
                <td>{b.size_kb} KB</td>
                <td className="muted xs">{new Date(b.created_at).toLocaleString('th-TH')}</td>
                <td className="row">
                  <button className="xs" onClick={() => downloadBackup(b)} disabled={busy === 'dl' + b.id}>
                    {busy === 'dl' + b.id && <InlineSpinner />}JSON
                  </button>
                  {b.storage_url && <a className="xs" href={b.storage_url} target="_blank" rel="noreferrer">Storage ↗</a>}
                </td>
              </tr>
            ))}
            {!backups.length && <tr><td colSpan={4} className="muted center">ยังไม่มี — กด "สำรองเดี๋ยวนี้"</td></tr>}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h3>การแจ้งเตือน + ตรวจสลิป</h3>
        <label className="row">
          <input type="checkbox" checked={settings.slip_notify_all_images !== false}
                 onChange={(e) => toggleSetting('slip_notify_all_images', e.target.checked)} />
          แจ้งแอดมิน (เข้า LINE) ทุกครั้งที่มีรูป/สลิปเข้ามา
        </label>
        <p className="muted xs">ปิด = แจ้งเฉพาะรูปหลังคุยเรื่องชำระเงิน · ต้องตั้ง env ALERT_USER_IDS · ตรวจสลิปอัตโนมัติ (ยอด/ธนาคาร/ซ้ำ) ต้องตั้ง env <code>EASYSLIP_TOKEN</code></p>

        <h4 className="sm">บัญชีรับเงิน (ใช้ match สลิปอัตโนมัติ)</h4>
        <table>
          <thead><tr><th>หลักสูตร</th><th>ธนาคาร</th><th>เลขบัญชี</th><th>ราคา</th><th>เต็ม</th><th></th></tr></thead>
          <tbody>
            {accts.map((a) => (
              <tr key={a.id}>
                <td>{a.course}</td><td>{a.bank}</td><td className="mono xs">{a.account_no}</td>
                <td>{a.price}</td><td className="muted">{a.full_price}</td>
                <td><button className="xs danger" onClick={() => confirm('ลบ?') && api.delPayAccount(a.id).then(loadSys)}>ลบ</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="row wrap" style={{ marginTop: 6 }}>
          <input placeholder="หลักสูตร" style={{ width: 80 }} value={na.course} onChange={(e) => setNa({ ...na, course: e.target.value })} />
          <input placeholder="ธนาคาร" style={{ width: 120 }} value={na.bank} onChange={(e) => setNa({ ...na, bank: e.target.value })} />
          <input placeholder="เลขบัญชี" style={{ width: 130 }} value={na.account_no} onChange={(e) => setNa({ ...na, account_no: e.target.value })} />
          <input placeholder="ราคา" type="number" style={{ width: 80 }} value={na.price} onChange={(e) => setNa({ ...na, price: e.target.value })} />
          <input placeholder="ราคาเต็ม" type="number" style={{ width: 80 }} value={na.full_price} onChange={(e) => setNa({ ...na, full_price: e.target.value })} />
          <button className="primary sm" onClick={addAcct}>เพิ่ม</button>
        </div>
      </section>

      <section className="card">
        <h3>หน้าลงทะเบียนสมาชิก (LIFF)</h3>
        <p className="muted xs">
          หน้า <code>{location.origin}/register</code> — สมาชิกเปิดผ่าน LIFF → ตรวจ userId กับทะเบียน · มีแล้ว→ไปหน้าถัดไป · ยังไม่มี→ต้องกรอกฟอร์มก่อน
          <br />ตั้ง Endpoint URL ของ LIFF (channel 2004722900) เป็น URL นี้ · env <code>REG_LIFF_CHANNEL_ID</code> = channel id (default 2004722900)
        </p>
        <label className="sm">URL หน้าถัดไป (หลังผ่านการตรวจ/ลงทะเบียน)</label>
        <input value={regNext} onChange={(e) => setRegNext(e.target.value)} placeholder="https://ecppquiz.glitch.me/..." />
        <label className="sm">หลักสูตรให้เลือก (คั่นด้วย , )</label>
        <input value={regCourses} onChange={(e) => setRegCourses(e.target.value)} placeholder="FC70, IC70, PC70-1, PC70-2" />
        <button className="primary sm" disabled={busy === 'reg'} onClick={async () => {
          setBusy('reg')
          try {
            await api.setSetting('reg_next_url', regNext.trim())
            await api.setSetting('reg_courses', regCourses.split(',').map((s) => s.trim()).filter(Boolean))
            t.ok('บันทึกแล้ว')
          } catch (e) { t.err(e.message) } finally { setBusy('') }
        }}>บันทึก</button>
      </section>

      <section className="card">
        <h3>Claude AI — ข้อมูลให้ AI ใช้ตอบ</h3>
        <p className="muted xs">
          AI (Claude) ตอบลูกค้าที่อยู่บนเมนูเปิด AI เฉพาะข้อความที่ไม่ตรงกฎไหน โดยใช้: ข้อความนี้ + ราคาคอร์ส/บัญชี + กฎตอบอัตโนมัติ (text/fallback) เป็นข้อมูล
          <br />ใส่: ตารางติว · ช่องทางติดต่อ · นโยบายคืนเงิน/เลื่อนคอร์ส · FAQ ที่ยังไม่ได้ทำเป็นกฎ
        </p>
        <textarea rows={8} value={gctx} onChange={(e) => setGctx(e.target.value)}
                  placeholder={'เช่น\nคอร์ส FC ติวทุกวันเสาร์ 9:00-16:00 ผ่าน Zoom เริ่ม 1 ก.พ.\nสอบถามเพิ่มเติม โทร 08x-xxx-xxxx\nโอนแล้วส่งสลิปในแชทนี้ ระบบตรวจอัตโนมัติ'} />
        <button className="primary sm" disabled={busy === 'gctx'}
                onClick={async () => {
                  setBusy('gctx')
                  try { await api.setSetting('gemini_context', gctx); t.ok('บันทึกแล้ว (มีผลใน ~5 นาที)') }
                  catch (e) { t.err(e.message) } finally { setBusy('') }
                }}>บันทึก</button>
      </section>

      <section className="card">
        <h3>ฟิลด์ข้อมูลเพิ่มเติมของผู้ใช้ (Custom fields)</h3>
        <table>
          <thead><tr><th>key</th><th>ชื่อ</th><th>ชนิด</th><th>ตัวเลือก</th><th></th></tr></thead>
          <tbody>
            {fields.map((f) => (
              <tr key={f.key}>
                <td className="mono xs">{f.key}</td><td>{f.label}</td><td>{f.type}</td>
                <td className="muted xs">{(f.options || []).join(', ')}</td>
                <td><button className="xs danger" onClick={() => confirm('ลบ field?') && api.delField(f.key).then(loadSys)}>ลบ</button></td>
              </tr>
            ))}
            {!fields.length && <tr><td colSpan={5} className="muted center">ยังไม่มี — เช่น คอร์ส, รุ่น, วันหมดอายุ</td></tr>}
          </tbody>
        </table>
        <div className="row wrap" style={{ marginTop: 8 }}>
          <input placeholder="key (a-z_)" value={nf.key} onChange={(e) => setNf({ ...nf, key: e.target.value })} style={{ width: 110 }} />
          <input placeholder="ชื่อที่แสดง" value={nf.label} onChange={(e) => setNf({ ...nf, label: e.target.value })} style={{ width: 130 }} />
          <select value={nf.type} onChange={(e) => setNf({ ...nf, type: e.target.value })}>
            <option value="text">ข้อความ</option><option value="number">ตัวเลข</option>
            <option value="date">วันที่</option><option value="select">ตัวเลือก</option>
          </select>
          {nf.type === 'select' && <input placeholder="ตัวเลือก คั่น ," value={nf.options} onChange={(e) => setNf({ ...nf, options: e.target.value })} style={{ width: 150 }} />}
          <button className="primary sm" onClick={addField}>เพิ่ม</button>
        </div>
      </section>

      <section className="card">
        <h3>ผู้ดูแล ({rows.length})</h3>
        <div className="table-scroll">
          <table>
            <thead><tr><th>userId</th><th>ชื่อ</th><th>role</th><th>เพิ่มเมื่อ</th><th></th></tr></thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.line_user_id}>
                  <td className="mono xs">{a.line_user_id}</td>
                  <td>{a.name}</td>
                  <td><span className="chip">{a.role}</span></td>
                  <td className="muted xs">{new Date(a.added_at).toLocaleDateString('th-TH')}</td>
                  <td>
                    {me.role === 'owner' && a.line_user_id !== me.userId && (
                      <button className="xs danger" onClick={() => confirm('ลบผู้ดูแลนี้?') && api.delAdmin(a.line_user_id).then(() => { t.ok('ลบแล้ว'); load() })}>ลบ</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {canEdit && (
        <section className="card">
          <h3>เพิ่มผู้ดูแล</h3>
          <div className="row wrap">
            <input placeholder="LINE userId (U...)" value={uid} onChange={(e) => setUid(e.target.value)} />
            <input placeholder="ชื่อ" value={name} onChange={(e) => setName(e.target.value)} />
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="admin">admin</option>
              <option value="viewer">viewer</option>
              <option value="owner">owner</option>
            </select>
            <button className="primary" onClick={add}>เพิ่ม</button>
          </div>
        </section>
      )}
    </div>
  )
}

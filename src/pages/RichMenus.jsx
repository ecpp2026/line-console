import { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { Spinner, useToast } from '../lib/ui.jsx'
import { useProgress } from '../lib/progress.jsx'
import RichMenuBuilder from '../components/RichMenuBuilder.jsx'

export default function RichMenus() {
  const t = useToast()
  const prog = useProgress()
  const [data, setData] = useState(null)
  const [usage, setUsage] = useState({})
  const [building, setBuilding] = useState(false)
  const [schedAt, setSchedAt] = useState('')
  const [busy, setBusy] = useState(false)
  const [sel, setSel] = useState('')
  const [target, setTarget] = useState('all')
  const [userIds, setUserIds] = useState('')
  const [setDefault, setSetDefault] = useState(true)
  const [geminiModal, setGeminiModal] = useState(null) // { rid, name, enabled, temperature }
  const [geminiBusy, setGeminiBusy] = useState(false)

  const load = () => {
    api.richmenus().then(setData).catch((e) => t.err(e.message))
    api.richmenuUsage()
      .then((d) => setUsage(Object.fromEntries(d.items.map((i) => [i.richMenuId || '__none__', i.count]))))
      .catch(() => {})
  }
  useEffect(() => { load() }, [])

  if (!data) return <Spinner />
  const { defaultRichMenuId, aliases } = data
  const menus = [...data.menus].sort((a, b) => (usage[b.richMenuId] || 0) - (usage[a.richMenuId] || 0))

  const parseIds = () =>
    userIds.split(/[\s,]+/).map((s) => s.trim()).filter((s) => s.startsWith('U'))

  const run = async (mode) => {
    if (mode === 'link' && !sel) return t.err('เลือก rich menu ก่อน')
    const label = { all: 'ผู้ใช้ทุกคน', none: 'คนที่ยังไม่มีเมนู', list: `${parseIds().length} คนในลิสต์` }[target] || target
    if (!confirm(`${mode === 'link' ? 'ผูก' : 'ถอด'} rich menu กับ ${label}?`)) return
    setBusy(true)
    const task = prog.start(mode === 'link' ? 'กำลังผูก Rich Menu' : 'กำลังถอด Rich Menu', 0)
    try {
      const { userIds: uids, count } = await api.resolveTarget(
        target === 'list' ? { target: 'list', userIds: parseIds() } : { target })
      if (!count) throw new Error('ไม่มีปลายทาง')
      task.setTotal(count)
      if (mode === 'link' && setDefault) await api.setDefaultMenu(sel)
      const CHUNK = 1500
      let ok = 0, fail = 0
      for (let i = 0; i < uids.length; i += CHUNK) {
        const r = await api.assignMenu({
          mode, richMenuId: sel, target: 'list', userIds: uids.slice(i, i + CHUNK),
        })
        ok += r.ok; fail += r.fail
        task.set(Math.min(i + CHUNK, count), `สำเร็จ ${ok.toLocaleString()} / ล้มเหลว ${fail}`)
      }
      t.ok(`เสร็จ: สำเร็จ ${ok.toLocaleString()} / ล้มเหลว ${fail} (จาก ${count.toLocaleString()})`)
      load()
    } catch (e) { t.err(e.message) } finally { task.done(); setBusy(false) }
  }

  const sync = async () => {
    setBusy(true)
    const task = prog.start('กำลัง Sync สถานะ Rich Menu ของผู้ใช้', 0)
    try {
      const { userIds: uids, count } = await api.resolveTarget({ target: 'all' })
      task.setTotal(count)
      const CHUNK = 1500
      let a = 0, n = 0, e = 0
      for (let i = 0; i < uids.length; i += CHUNK) {
        const r = await api.syncMenu({ target: 'list', userIds: uids.slice(i, i + CHUNK) })
        a += r.assigned; n += r.none; e += r.error || 0
        task.set(Math.min(i + CHUNK, count), `มีเมนู ${a.toLocaleString()} / ไม่มี ${n.toLocaleString()}`)
      }
      t.ok(`sync เสร็จ: มีเมนู ${a.toLocaleString()} / ไม่มี ${n.toLocaleString()} / error ${e}`)
      load()
    } catch (err) { t.err(err.message) } finally { task.done(); setBusy(false) }
  }

  const saveGemini = async () => {
    if (!geminiModal) return
    setGeminiBusy(true)
    try {
      await api.setMenuGemini(geminiModal.rid, {
        enabled: geminiModal.enabled, temperature: geminiModal.temperature,
      })
      t.ok('บันทึกการตั้งค่า AI แล้ว')
      setGeminiModal(null)
      load()
    } catch (e) { t.err(e.message) } finally { setGeminiBusy(false) }
  }

  return (
    <div>
      <h1>Rich Menu</h1>

      <section className="card">
        <div className="row spread">
          <h3>รายการเมนู ({menus.length})</h3>
          <div className="row">
            <button className="sm primary" onClick={() => setBuilding(true)}>+ สร้างเมนูใหม่</button>
            <button className="sm" onClick={sync} disabled={busy}>Sync สถานะผู้ใช้ทั้งหมด</button>
          </div>
        </div>
        <table>
          <thead><tr><th></th><th>ชื่อ</th><th>ใช้อยู่</th><th>chatBar</th><th>ขนาด</th><th>richMenuId</th><th></th></tr></thead>
          <tbody>
            {menus.map((m) => (
              <tr key={m.richMenuId} className={sel === m.richMenuId ? 'selected' : ''}>
                <td><input type="radio" checked={sel === m.richMenuId}
                           onChange={() => setSel(m.richMenuId)} /></td>
                <td>{m.name} {m.richMenuId === defaultRichMenuId && <span className="chip ok">default</span>}</td>
                <td><b>{(usage[m.richMenuId] || 0).toLocaleString()}</b></td>
                <td>{m.chatBarText}</td>
                <td className="muted sm">{m.size?.width}×{m.size?.height}</td>
                <td className="mono xs">{m.richMenuId}</td>
                <td className="row">
                  <button className="xs" onClick={() => api.setDefaultMenu(m.richMenuId).then(() => { t.ok('ตั้ง default'); load() }).catch((e) => t.err(e.message))}>
                    ตั้ง default
                  </button>
                  <button className={`xs ${m.geminiEnabled ? 'primary' : ''}`}
                          title="ตั้งค่า Claude AI สำหรับเมนูนี้"
                          onClick={() => setGeminiModal({
                            rid: m.richMenuId, name: m.name,
                            enabled: !!m.geminiEnabled,
                            temperature: typeof m.geminiTemperature === 'number' ? m.geminiTemperature : 0.7,
                          })}>
                    🤖 AI{m.geminiEnabled ? ' ✓' : ''}
                  </button>
                  <button className="xs danger" onClick={() => confirm('ลบเมนูนี้?') && api.deleteMenu(m.richMenuId).then(() => { t.ok('ลบแล้ว'); load() }).catch((e) => t.err(e.message))}>
                    ลบ
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {defaultRichMenuId && (
          <p className="muted sm">
            default ปัจจุบัน: <code>{defaultRichMenuId}</code>{' '}
            <button className="xs" onClick={() => api.clearDefaultMenu().then(() => { t.ok('ล้าง default'); load() })}>ล้าง</button>
          </p>
        )}
        {!!aliases.length && (
          <p className="muted sm">alias: {aliases.map((a) => `${a.richMenuAliasId}→${a.richMenuId.slice(-6)}`).join(' , ')}</p>
        )}
      </section>

      <section className="card">
        <h3>ผูก / ถอด เป็นชุด</h3>
        <div className="row wrap">
          <label>ปลายทาง:&nbsp;
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="all">ผู้ใช้ที่ติดตามทุกคน</option>
              <option value="none">เฉพาะคนที่ยังไม่มีเมนู</option>
              <option value="list">ระบุ userId เอง</option>
            </select>
          </label>
          <label><input type="checkbox" checked={setDefault} onChange={(e) => setSetDefault(e.target.checked)} /> ตั้งเป็น default ด้วย</label>
        </div>
        {target === 'list' && (
          <textarea rows={5} placeholder="วาง userId (คั่นด้วยขึ้นบรรทัด/comma)"
                    value={userIds} onChange={(e) => setUserIds(e.target.value)} />
        )}
        <div className="row">
          <button className="primary" disabled={busy} onClick={() => run('link')}>
            ผูกเมนูที่เลือก {sel && `(${menus.find((m) => m.richMenuId === sel)?.name})`}
          </button>
          <button disabled={busy} onClick={() => run('unlink')}>ถอดเมนู</button>
        </div>
      </section>

      <section className="card">
        <h3>ตั้งเวลาสลับ Default Menu</h3>
        <p className="muted xs">เลือกเมนูด้านบน + เวลา → ระบบจะตั้งเป็น default menu ให้อัตโนมัติ (เช็คทุก 5 นาที)</p>
        <div className="row wrap">
          <input type="datetime-local" value={schedAt} onChange={(e) => setSchedAt(e.target.value)} />
          <button className="sm" disabled={!sel || !schedAt} onClick={async () => {
            try {
              await api.schedule({ kind: 'richmenu_default', richMenuId: sel, runAt: new Date(schedAt).toISOString(),
                                   label: `default → ${menus.find((m) => m.richMenuId === sel)?.name}` })
              t.ok('ตั้งเวลาแล้ว'); setSchedAt('')
            } catch (e) { t.err(e.message) }
          }}>ตั้งเวลา</button>
        </div>
      </section>

      {building && <RichMenuBuilder onCancel={() => setBuilding(false)} onDone={() => { setBuilding(false); load() }} />}

      {geminiModal && (
        <div className="modal-bg" onClick={() => setGeminiModal(null)}>
          <div className="modal" style={{ maxWidth: 480 }} onClick={(e) => e.stopPropagation()}>
            <div className="row spread">
              <h3>🤖 ตั้งค่า AI — {geminiModal.name}</h3>
              <button className="xs" onClick={() => setGeminiModal(null)}>✕</button>
            </div>

            <label>
              <input type="checkbox" checked={geminiModal.enabled}
                     onChange={(e) => setGeminiModal({ ...geminiModal, enabled: e.target.checked })} />
              {' '}เปิดให้ AI ตอบข้อความอัตโนมัติ เมื่อ user ที่ถือเมนูนี้ทักเข้ามา
            </label>

            <div style={{ marginTop: 14, opacity: geminiModal.enabled ? 1 : 0.5 }}>
              <label className="sm">ความเข้มข้นของคำตอบ (ความคิดสร้างสรรค์)</label>
              <div className="row" style={{ alignItems: 'center', gap: 10 }}>
                <input type="range" min={0} max={2} step={0.1} style={{ flex: 1 }}
                       disabled={!geminiModal.enabled}
                       value={geminiModal.temperature}
                       onChange={(e) => setGeminiModal({ ...geminiModal, temperature: parseFloat(e.target.value) })} />
                <b className="mono" style={{ minWidth: 36, textAlign: 'right' }}>{geminiModal.temperature.toFixed(1)}</b>
              </div>
              <div className="row spread muted xs" style={{ marginTop: 2 }}>
                <span>0.0 นิ่ง/แม่นยำ</span>
                <span>1.0 ปานกลาง</span>
                <span>2.0 สร้างสรรค์มาก</span>
              </div>
              <div className="row wrap" style={{ marginTop: 8 }}>
                {[
                  { v: 0.2, label: 'นิ่งมาก' },
                  { v: 0.7, label: 'ปานกลาง (แนะนำ)' },
                  { v: 1.3, label: 'สร้างสรรค์' },
                ].map((p) => (
                  <button key={p.v} type="button" className="xs"
                          disabled={!geminiModal.enabled}
                          onClick={() => setGeminiModal({ ...geminiModal, temperature: p.v })}>
                    {p.label}
                  </button>
                ))}
              </div>
            </div>

            <p className="muted xs" style={{ marginTop: 12 }}>
              ค่ายิ่งสูง คำตอบจะยิ่งหลากหลาย/มีสไตล์มากขึ้น แต่มีโอกาสหลุดประเด็นมากขึ้นด้วย — ค่ายิ่งต่ำ คำตอบจะนิ่งและตรงไปตรงมามากขึ้น
            </p>

            <div className="row spread" style={{ marginTop: 12 }}>
              <button onClick={() => setGeminiModal(null)}>ยกเลิก</button>
              <button className="primary" disabled={geminiBusy} onClick={saveGemini}>
                {geminiBusy ? 'กำลังบันทึก…' : 'บันทึก'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

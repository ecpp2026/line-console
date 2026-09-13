import { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { Spinner, InlineSpinner, useToast } from '../lib/ui.jsx'

const EMPTY = { title: '', body: '', keywords: '', category: '', enabled: true }

export default function KB() {
  const t = useToast()
  const [list, setList] = useState(null)
  const [hint, setHint] = useState('')
  const [form, setForm] = useState(null)
  const [busy, setBusy] = useState(false)
  const [gaps, setGaps] = useState(null)
  const [showGaps, setShowGaps] = useState(false)

  const load = () => api.kb().then((d) => { setList(d.articles); if (d.schema_missing) setHint(d.hint) }).catch((e) => t.err(e.message))
  useEffect(() => { load() }, [])
  useEffect(() => {
    if (showGaps && !gaps) api.kbGaps(14).then(setGaps).catch((e) => t.err(e.message))
  }, [showGaps]) // eslint-disable-line

  const articleFromQuestion = (q) => setForm({
    ...EMPTY, title: q.length > 60 ? q.slice(0, 60) + '…' : q, keywords: q,
  })

  const save = async () => {
    if (!form.title.trim() || !form.body.trim()) return t.err('ใส่หัวข้อ + เนื้อหา')
    setBusy(true)
    try {
      await api.saveKb({ ...form, keywords: form.keywords.split(',').map((s) => s.trim()).filter(Boolean) })
      t.ok('บันทึกแล้ว (AI ใช้ใน ~5 นาที)'); setForm(null); load()
    } catch (e) { t.err(e.message) } finally { setBusy(false) }
  }

  const byCat = {}
  ;(list || []).forEach((a) => { (byCat[a.category || 'ทั่วไป'] = byCat[a.category || 'ทั่วไป'] || []).push(a) })

  return (
    <div>
      <div className="row spread wrap" style={{ alignItems: 'center' }}>
        <h1 style={{ margin: 0 }}>คลังความรู้</h1>
        <button className="sm primary" onClick={() => setForm({ ...EMPTY })}>+ บทความ</button>
      </div>
      <p className="muted sm">
        แหล่งข้อมูลกลางที่ <b>Claude AI</b> ดึงไปใช้ตอบลูกค้า (ค้นตามคำถามอัตโนมัติ) — ใส่: รายละเอียดคอร์ส · ตารางติว · เงื่อนไข · วิธีชำระ · FAQ ต่าง ๆ
      </p>

      {hint && <p className="err">{hint}</p>}

      <section className="card">
        <div className="row spread">
          <h3>🤖 คำถามที่ AI ยังตอบไม่ได้ดี</h3>
          <button className="sm" onClick={() => setShowGaps((v) => !v)}>{showGaps ? 'ซ่อน' : 'วิเคราะห์ 14 วัน'}</button>
        </div>
        {showGaps && (!gaps ? <Spinner /> : (
          <>
            <p className="muted sm">
              คำถามที่ AI ตอบไปแล้ว แต่ <b>ไม่เจอบทความ/FAQ ที่เกี่ยวข้องเลย</b> ({gaps.total.toLocaleString()} ครั้งใน {gaps.days} วัน) — เพิ่มบทความให้ AI ฉลาดขึ้น
            </p>
            <div className="grid two">
              <div>
                <h4 className="sm">คำถามซ้ำบ่อย</h4>
                <table>
                  <tbody>
                    {gaps.top_questions.slice(0, 15).map((q, i) => (
                      <tr key={i}>
                        <td>{q.text}</td>
                        <td className="muted">{q.count}×</td>
                        <td><button className="xs primary" onClick={() => articleFromQuestion(q.text)}>สร้างบทความ</button></td>
                      </tr>
                    ))}
                    {!gaps.top_questions.length && <tr><td className="muted">ยังไม่มีข้อมูลพอ</td></tr>}
                  </tbody>
                </table>
              </div>
              <div>
                <h4 className="sm">คำที่โผล่บ่อย</h4>
                <div className="row wrap" style={{ gap: 5 }}>
                  {gaps.top_keywords.map((k, i) => (
                    <button key={i} className="xs" onClick={() => articleFromQuestion(k.word)}>
                      {k.word} <span className="muted">{k.count}</span>
                    </button>
                  ))}
                  {!gaps.top_keywords.length && <span className="muted sm">—</span>}
                </div>
              </div>
            </div>
          </>
        ))}
      </section>

      {!list ? <Spinner /> : list.length === 0 && !hint ? (
        <p className="muted card center">ยังไม่มีบทความ — กด "+ บทความ" เพิ่มความรู้ให้ AI</p>
      ) : (
        Object.entries(byCat).map(([cat, arts]) => (
          <section className="card" key={cat}>
            <h3>{cat} ({arts.length})</h3>
            {arts.map((a) => (
              <div key={a.id} className="barlist-row" style={{ gridTemplateColumns: '1fr auto', alignItems: 'start', padding: '8px 0', borderBottom: '1px solid var(--border)' }}>
                <div style={{ opacity: a.enabled ? 1 : 0.5 }}>
                  <b>{a.title}</b>
                  <div className="muted xs">{a.body.slice(0, 120)}{a.body.length > 120 ? '…' : ''}</div>
                  {a.keywords?.length > 0 && <div className="row wrap" style={{ gap: 3, marginTop: 3 }}>
                    {a.keywords.map((k) => <span key={k} className="chip xs">{k}</span>)}
                  </div>}
                </div>
                <div className="row" style={{ gap: 4 }}>
                  <button className="xs" onClick={() => setForm({ ...a, keywords: (a.keywords || []).join(', ') })}>แก้</button>
                  <button className="xs danger" onClick={() => confirm('ลบ?') && api.delKb(a.id).then(load)}>ลบ</button>
                </div>
              </div>
            ))}
          </section>
        ))
      )}

      {form && (
        <div className="modal-bg" onClick={() => setForm(null)}>
          <div className="modal" style={{ maxWidth: 600 }} onClick={(e) => e.stopPropagation()}>
            <div className="row spread"><h3>{form.id ? 'แก้ไขบทความ' : 'บทความใหม่'}</h3><button className="xs" onClick={() => setForm(null)}>✕</button></div>
            <label className="sm">หัวข้อ *</label>
            <input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="เช่น ตารางเรียนคอร์ส FC70" />
            <label className="sm">เนื้อหา * (AI จะใช้ตอบตามนี้)</label>
            <textarea rows={6} value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })}
                      placeholder="คอร์ส FC70 เรียนทุกวันเสาร์ 9:00-16:00 ผ่าน Zoom เริ่ม 1 ก.พ. 2570 มีทั้งหมด 8 ครั้ง..." />
            <div className="row wrap">
              <label className="sm" style={{ flex: 1 }}>หมวด<input value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} placeholder="ตารางเรียน / ราคา / เงื่อนไข" /></label>
            </div>
            <label className="sm">คำค้น (คั่นด้วย , — ช่วยให้ AI หาเจอ)</label>
            <input value={form.keywords} onChange={(e) => setForm({ ...form, keywords: e.target.value })} placeholder="ตารางเรียน, เริ่มเรียน, วันไหน, FC70" />
            <label className="row" style={{ marginTop: 6 }}><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> เปิดใช้</label>
            <button className="primary" disabled={busy} onClick={save}>{busy && <InlineSpinner />}บันทึก</button>
          </div>
        </div>
      )}
    </div>
  )
}

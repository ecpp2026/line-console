import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  LineChart, Line, BarChart, Bar, AreaChart, Area, PieChart, Pie, Cell,
  XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend,
} from 'recharts'
import { api } from '../lib/api.js'
import { SkeletonStats, SkeletonRows, InlineSpinner, Stat, useToast } from '../lib/ui.jsx'

/* ปาเลตต์หมวดหมู่ — ลำดับคงที่ ไม่วนซ้ำ */
const CAT = ['#06c755', '#2563eb', '#d97706', '#7c3aed', '#0891b2', '#db2777', '#65a30d', '#64748b']
const ST = { verified: '#06c755', review: '#d97706', rejected: '#e5484d', new: '#2563eb' }
const AXIS = { fontSize: 11, fill: 'var(--muted)' }
const GRID = 'var(--border)'
const mmdd = (d) => (d || '').slice(5)
const nf = (n) => (n == null ? '–' : Number(n).toLocaleString('th-TH'))
const baht = (n) => (n == null ? '–' : '฿' + Number(n).toLocaleString('th-TH'))
const dtf = (s) => new Date(s).toLocaleString('th-TH', { dateStyle: 'short', timeStyle: 'short' })

const SECTIONS = [
  ['summary', 'สรุป'],
  ['people', 'ผู้ใช้ & การเติบโต'],
  ['revenue', 'รายได้ & สลิป'],
  ['messages', 'ข้อความ & แชท'],
  ['bot', 'บอท & อัตโนมัติ'],
  ['system', 'ระบบ'],
]

export default function Dashboard() {
  const t = useToast()
  const nav = useNavigate()
  const [range, setRange] = useState(30)
  const [d, setD] = useState(null)
  const [a, setA] = useState(null)
  const [err, setErr] = useState('')

  const load = (r = range) => {
    setD(null); setA(null); setErr('')
    api.dashboard(r).then(setD).catch((e) => setErr(e.message))
    api.dashboardAnalytics(r).then(setA).catch(() => {})
  }
  useEffect(() => { load(range) }, [range]) // eslint-disable-line

  const jump = (id) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  if (err) return <p className="err">{err}</p>

  return (
    <div className="dash">
      <div className="row spread wrap" style={{ alignItems: 'center' }}>
        <h1 style={{ margin: 0 }}>แดชบอร์ด</h1>
        <div className="row wrap" style={{ gap: 6 }}>
          {[7, 14, 30, 60, 90].map((r) => (
            <button key={r} className={range === r ? 'sm primary' : 'sm'} onClick={() => setRange(r)}>{r} วัน</button>
          ))}
          <button className="sm" onClick={() => load()} title="รีเฟรช">↻</button>
        </div>
      </div>
      {d
        ? <div className="muted xs" style={{ margin: '2px 0 8px' }}>
            อัปเดต {dtf(d.generated_at)} · deploy <code>{d.commit}</code> · ช่วง {d.range_days} วัน
            {!a && <> · <InlineSpinner />กำลังโหลดกราฟเชิงลึก…</>}
          </div>
        : <div className="muted xs" style={{ margin: '2px 0 8px' }}>กำลังโหลด…</div>}

      <nav className="dash-nav">
        {SECTIONS.map(([id, label]) => (
          <button key={id} onClick={() => jump(id)}>{label}</button>
        ))}
      </nav>

      {!d ? (
        <>
          <SkeletonStats />
          <div className="card"><SkeletonRows rows={6} cols={2} /></div>
          <div className="loading-text center"><InlineSpinner />กำลังโหลดแดชบอร์ด…</div>
        </>
      ) : (
        <>
          <Summary d={d} a={a} nav={nav} />
          <People d={d} a={a} />
          <Revenue d={d} a={a} nav={nav} />
          <Messages d={d} a={a} />
          <Bot d={d} a={a} />
          <System d={d} a={a} />
        </>
      )}
    </div>
  )
}

/* ---------- helpers ---------- */
function H({ id, children, sub }) {
  return <div className="dash-h" id={id}><h2>{children}</h2>{sub && <span className="muted sm">{sub}</span>}</div>
}
function Card({ title, sub, children, right }) {
  return (
    <section className="card">
      {title && (
        <div className="row spread" style={{ alignItems: 'baseline' }}>
          <h3 style={{ margin: '0 0 2px' }}>{title}</h3>
          {right}
        </div>
      )}
      {sub && <p className="muted xs" style={{ margin: '0 0 8px' }}>{sub}</p>}
      {children}
    </section>
  )
}
const ttStyle = { background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12, boxShadow: 'var(--shadow)' }
const Empty = () => <p className="muted sm center" style={{ padding: '18px 0', minHeight: 'auto' }}>ยังไม่มีข้อมูล</p>

function TimeArea({ data, keys, height = 210 }) {
  if (!data?.length) return <Empty />
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 4, right: 8, left: -18, bottom: 0 }}>
        <defs>{keys.map((s) => (
          <linearGradient key={s.k} id={`g-${s.k}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={s.color} stopOpacity={0.28} />
            <stop offset="100%" stopColor={s.color} stopOpacity={0.02} />
          </linearGradient>
        ))}</defs>
        <CartesianGrid strokeDasharray="3 3" stroke={GRID} vertical={false} />
        <XAxis dataKey="day" tickFormatter={mmdd} tick={AXIS} minTickGap={24} />
        <YAxis tick={AXIS} width={44} allowDecimals={false} />
        <Tooltip contentStyle={ttStyle} labelFormatter={(l) => `วันที่ ${l}`} />
        {keys.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {keys.map((s) => (
          <Area key={s.k} type="monotone" dataKey={s.k} name={s.name} stroke={s.color}
                strokeWidth={2} fill={`url(#g-${s.k})`} dot={false} />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  )
}
function StackBars({ data, keys, height = 210 }) {
  if (!data?.length) return <Empty />
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 4, right: 8, left: -18, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={GRID} vertical={false} />
        <XAxis dataKey="day" tickFormatter={mmdd} tick={AXIS} minTickGap={24} />
        <YAxis tick={AXIS} width={44} allowDecimals={false} />
        <Tooltip contentStyle={ttStyle} labelFormatter={(l) => `วันที่ ${l}`} />
        {keys.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {keys.map((s, i) => (
          <Bar key={s.k} dataKey={s.k} name={s.name} stackId="a" fill={s.color}
               radius={i === keys.length - 1 ? [3, 3, 0, 0] : 0} maxBarSize={26} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  )
}
function BarList({ rows, labelKey, valueKey, fmt = nf, color = CAT[1] }) {
  if (!rows?.length) return <Empty />
  const max = Math.max(...rows.map((r) => r[valueKey] || 0), 1)
  return (
    <div className="barlist">
      {rows.map((r, i) => (
        <div key={i} className="barlist-row">
          <span className="barlist-label" title={r[labelKey]}>{r[labelKey]}</span>
          <span className="barlist-track">
            <span className="barlist-fill" style={{
              width: `${((r[valueKey] || 0) / max) * 100}%`,
              background: Array.isArray(color) ? color[i % color.length] : color,
            }} />
          </span>
          <span className="barlist-val">{fmt(r[valueKey])}</span>
        </div>
      ))}
    </div>
  )
}
function Donut({ rows, nameKey, valueKey, colors = CAT, height = 190 }) {
  const data = (rows || []).filter((r) => (r[valueKey] || 0) > 0)
  if (!data.length) return <Empty />
  const total = data.reduce((s, r) => s + (r[valueKey] || 0), 0)
  return (
    <div className="row" style={{ alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
      <ResponsiveContainer width={height} height={height}>
        <PieChart>
          <Pie data={data} dataKey={valueKey} nameKey={nameKey} innerRadius="58%" outerRadius="92%"
               paddingAngle={2} stroke="var(--panel)" strokeWidth={2}>
            {data.map((r, i) => <Cell key={i} fill={typeof colors === 'function' ? colors(r) : colors[i % colors.length]} />)}
          </Pie>
          <Tooltip contentStyle={ttStyle} formatter={(v) => nf(v)} />
        </PieChart>
      </ResponsiveContainer>
      <div className="donut-legend">
        {data.map((r, i) => (
          <div key={i} className="row" style={{ gap: 6, fontSize: 12 }}>
            <span className="dot-sq" style={{ background: typeof colors === 'function' ? colors(r) : colors[i % colors.length] }} />
            <span style={{ flex: 1 }}>{r[nameKey]}</span>
            <b>{nf(r[valueKey])}</b><span className="muted">{Math.round((r[valueKey] / total) * 100)}%</span>
          </div>
        ))}
      </div>
    </div>
  )
}
function Funnel({ steps }) {
  if (!steps?.length) return <Empty />
  const max = steps[0].count || 1
  return (
    <div className="barlist">
      {steps.map((s, i) => {
        const prev = i ? steps[i - 1].count : s.count
        const drop = prev ? Math.round((s.count / prev) * 100) : 100
        return (
          <div key={i} className="barlist-row">
            <span className="barlist-label">{s.step}</span>
            <span className="barlist-track">
              <span className="barlist-fill" style={{ width: `${(s.count / max) * 100}%`, background: CAT[i % CAT.length] }} />
            </span>
            <span className="barlist-val">{nf(s.count)} {i > 0 && <span className="muted">({drop}%)</span>}</span>
          </div>
        )
      })}
    </div>
  )
}
function Heatmap({ grid }) {
  const DOW = ['จ', 'อ', 'พ', 'พฤ', 'ศ', 'ส', 'อา']
  const flat = (grid || []).flat()
  const max = Math.max(...flat, 1)
  if (!flat.some((x) => x > 0)) return <Empty />
  return (
    <div className="heatmap">
      <div className="heat-hours"><span />{[0, 3, 6, 9, 12, 15, 18, 21].map((h) => <span key={h}>{h}</span>)}</div>
      {(grid || []).map((row, di) => (
        <div key={di} className="heat-row">
          <span className="heat-dow">{DOW[di]}</span>
          {row.map((v, hi) => (
            <span key={hi} className="heat-cell" title={`${DOW[di]} ${hi}:00 — ${v}`}
                  style={{ background: v ? `rgba(6,199,85,${0.12 + (v / max) * 0.8})` : 'var(--panel-2,#f1f3f6)' }} />
          ))}
        </div>
      ))}
    </div>
  )
}
function PeopleList({ rows, valueLabel = '' }) {
  if (!rows?.length) return <Empty />
  return (
    <ul className="loglist">
      {rows.map((r, i) => (
        <li key={r.line_user_id || i}>
          {r.picture_url
            ? <img className="avatar-xs" src={r.picture_url} alt="" />
            : <span className="dot" />}
          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {r.display_name || (r.line_user_id || '').slice(0, 12)}
          </span>
          <b>{nf(r.count)}</b><span className="muted xs">{valueLabel}</span>
        </li>
      ))}
    </ul>
  )
}
function OpsList({ ops }) {
  return (
    <ul className="loglist">
      {(ops || []).map((o) => (
        <li key={o.id}>
          <span className={`dot ${o.status}`} />{o.action}
          <span className="muted xs"> · {o.actor?.slice(0, 8) || '–'} · {dtf(o.created_at)}</span>
        </li>
      ))}
      {!ops?.length && <li className="muted">ยังไม่มี</li>}
    </ul>
  )
}

/* ---------- 1. สรุป ---------- */
function Summary({ d, a, nav }) {
  const s = d.slips, sys = d.system, u = d.users, c = d.counts || {}
  return (
    <>
      <H id="summary">สรุป</H>
      <div className="grid stats">
        <Stat label="ผู้ติดตาม" value={nf(u.following)} sub={`${u.follow_rate}% · +${d.growth_totals.net} ช่วงนี้`} />
        <Stat label="แอคทีฟ 30 วัน" value={nf(u.active_30d)} sub={`${u.active_rate}% ของผู้ติดตาม`} />
        <Stat label="ผู้ใช้ใหม่ 7 วัน" value={nf(u.new_7d)} sub={`30 วัน ${nf(u.new_30d)}`} />
        <Stat label="สลิปรอตรวจ" value={nf(s.pending_action)} sub={s.pending_action ? 'ต้องดำเนินการ' : 'เคลียร์หมด'} />
        <Stat label="รายได้ยืนยันแล้ว" value={baht(s.revenue_verified)} sub={`สัปดาห์นี้ ${baht(s.revenue_week)}`} />
        <Stat label="แชทค้างตอบ" value={nf(u.unread_threads)} />
        <Stat label="ข้อความวันนี้" value={nf(d.messages.today)} sub={`เข้า 7 วัน ${nf(d.messages.in_7d)}`} />
        <Stat label="โควตาข้อความ" value={sys.quota_pct != null ? sys.quota_pct + '%' : nf(sys.quota_used)}
              sub={`${nf(sys.quota_used)} / ${nf(sys.quota_limit)}`} />
        <Stat label="Error 24 ชม." value={nf(sys.errors_24h)} sub={sys.errors_24h ? '⚠ ตรวจสอบ' : 'ปกติ'} />
        <Stat label="AI ตอบ 7 วัน" value={nf(d.messages.gemini_7d)} />
      </div>

      <Card title="Quick actions">
        <div className="row wrap">
          <button className="sm primary" onClick={() => nav('/messaging')}>ส่งข้อความ</button>
          <button className="sm" onClick={() => nav('/slips')}>ตรวจสลิป {s.pending_action ? `(${s.pending_action})` : ''}</button>
          <button className="sm" onClick={() => nav('/inbox')}>กล่องข้อความ {u.unread_threads ? `(${u.unread_threads})` : ''}</button>
          <button className="sm" onClick={() => nav('/tasks')}>งาน {sys.tasks_overdue ? `(⚠${sys.tasks_overdue})` : sys.tasks_open ? `(${sys.tasks_open})` : ''}</button>
          <button className="sm" onClick={() => nav('/richmenus')}>Rich Menu</button>
          <button className="sm" onClick={() => nav('/auto-reply')}>ตอบอัตโนมัติ</button>
          <button className="sm" onClick={() => nav('/stats')}>สถิติ LINE</button>
        </div>
      </Card>

      <Card title="องค์ประกอบระบบ">
        <div className="chip-grid">
          <span className="mchip">Rich Menu <b>{nf(c.rich_menus)}</b></span>
          <span className="mchip">กฎตอบอัตโนมัติ <b>{nf(c.auto_replies_on)}/{nf(c.auto_replies)}</b></span>
          <span className="mchip">Postback <b>{nf(c.postbacks_on)}/{nf(c.postbacks)}</b></span>
          <span className="mchip">Automation <b>{nf(c.automations_on)}/{nf(c.automations)}</b></span>
          <span className="mchip">กลุ่มเป้าหมาย <b>{nf(c.segments)}</b></span>
          <span className="mchip">เทมเพลต <b>{nf(c.templates)}</b></span>
          <span className="mchip">ลิงก์สั้น <b>{nf(c.links)}</b></span>
          <span className="mchip">งานตั้งเวลา <b>{nf(c.scheduled_pending)}</b></span>
          <span className="mchip">เคยบล็อก <b>{nf(c.blocked_ever)}</b></span>
        </div>
      </Card>

      <div className="grid two">
        <Card title="การเติบโตผู้ติดตาม" sub={`+${d.growth_totals.follow} / -${d.growth_totals.unfollow} = สุทธิ ${d.growth_totals.net >= 0 ? '+' : ''}${d.growth_totals.net}`}>
          <StackBars data={d.growth} keys={[
            { k: 'follow', name: 'เพิ่มเพื่อน', color: ST.verified },
            { k: 'unfollow', name: 'บล็อก/ลบ', color: ST.rejected },
          ]} />
        </Card>
        <Card title="เส้นทางลูกค้า (Funnel)">
          <Funnel steps={a?.funnel} />
        </Card>
      </div>
    </>
  )
}

/* ---------- 2. ผู้ใช้ ---------- */
function People({ d, a }) {
  const u = d.users
  return (
    <>
      <H id="people">ผู้ใช้ & การเติบโต</H>
      <div className="grid stats">
        <Stat label="ผู้ใช้ในระบบ" value={nf(u.total)} />
        <Stat label="กำลังติดตาม" value={nf(u.following)} sub={`${u.follow_rate}%`} />
        <Stat label="เลิกติดตาม" value={nf(u.not_following)} />
        <Stat label="มี Rich Menu" value={nf(u.with_menu)} sub={`ไม่มี ${nf(u.no_menu)}`} />
        <Stat label="แอคทีฟ 7 วัน" value={nf(u.active_7d)} sub={`30 วัน ${nf(u.active_30d)}`} />
        <Stat label="ผู้ใช้ใหม่ 30 วัน" value={nf(u.new_30d)} />
      </div>

      <div className="grid two">
        <Card title="ผู้ใช้ใหม่ / วัน">
          <StackBars data={d.new_daily} keys={[{ k: 'count', name: 'ผู้ใช้ใหม่', color: CAT[0] }]} />
        </Card>
        <Card title="ผู้ติดตามสะสม (สุทธิ)" sub="เทียบต้นช่วง">
          <TimeArea data={d.growth} keys={[{ k: 'cumulative', name: 'สุทธิสะสม', color: CAT[1] }]} />
        </Card>
      </div>

      {u.pipeline?.some((p) => p.count > 0) && (
        <Card title="Pipeline ลูกค้า" sub="ตั้งสถานะได้ในหน้ารายละเอียดผู้ใช้">
          <BarList rows={u.pipeline.filter((p) => p.count > 0)} labelKey="label" valueKey="count" color={CAT} />
        </Card>
      )}

      <div className="grid two">
        <Card title="แหล่งที่มาผู้ใช้">
          <BarList rows={u.source_breakdown} labelKey="source" valueKey="count" color={CAT} />
        </Card>
        <Card title="การกระจาย Rich Menu" sub="เฉพาะผู้ที่ติดตาม">
          <BarList rows={d.menu_distribution.map((m) => ({ ...m, name: m.name + (m.is_default ? ' •default' : '') }))}
                   labelKey="name" valueKey="count" color={CAT[3]} />
        </Card>
      </div>

      {a?.demographic?.genders?.length > 0 && (
        <div className="grid two">
          <Card title="เพศ (LINE insight)">
            <ResponsiveContainer width="100%" height={190}>
              <BarChart data={a.demographic.genders} margin={{ left: -20 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={GRID} vertical={false} />
                <XAxis dataKey="gender" tick={AXIS} /><YAxis tick={AXIS} unit="%" />
                <Tooltip contentStyle={ttStyle} formatter={(v) => v + '%'} />
                <Bar dataKey="percentage" fill={CAT[0]} radius={[3, 3, 0, 0]} maxBarSize={60} />
              </BarChart>
            </ResponsiveContainer>
          </Card>
          <Card title="อายุ (LINE insight)">
            <ResponsiveContainer width="100%" height={190}>
              <BarChart data={a.demographic.ages} margin={{ left: -20 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={GRID} vertical={false} />
                <XAxis dataKey="age" tick={{ ...AXIS, fontSize: 9 }} /><YAxis tick={AXIS} unit="%" />
                <Tooltip contentStyle={ttStyle} formatter={(v) => v + '%'} />
                <Bar dataKey="percentage" fill={CAT[1]} radius={[3, 3, 0, 0]} maxBarSize={40} />
              </BarChart>
            </ResponsiveContainer>
          </Card>
        </div>
      )}

      {a?.history?.length > 1 && (
        <Card title="แนวโน้มผู้ติดตาม (LINE insight)" sub="lag 1-2 วัน">
          <TimeArea data={a.history} keys={[
            { k: 'followers', name: 'ผู้ติดตาม', color: CAT[0] },
            { k: 'targeted_reaches', name: 'เข้าถึงได้', color: CAT[1] },
            { k: 'blocks', name: 'บล็อก', color: ST.rejected },
          ]} height={230} />
        </Card>
      )}

      <Card title="follow / unfollow ล่าสุด">
        <ul className="loglist">
          {(d.recent_follows || []).map((f, i) => (
            <li key={i}>
              {f.picture_url ? <img className="avatar-xs" src={f.picture_url} alt="" /> : <span className={`dot ${f.action === 'follow' ? 'ok' : 'error'}`} />}
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {f.display_name || (f.line_user_id || '').slice(0, 12)}
              </span>
              <span className={`chip xs ${f.action === 'follow' ? 'ok' : 'failed'}`}>
                {f.action === 'follow' ? (f.is_unblocked ? 'unblock' : 'follow') : 'unfollow'}
              </span>
              <span className="muted xs">{dtf(f.event_ts)}</span>
            </li>
          ))}
          {!d.recent_follows?.length && <li className="muted">ยังไม่มี</li>}
        </ul>
      </Card>
    </>
  )
}

/* ---------- 3. รายได้ / สลิป ---------- */
function Revenue({ d, a, nav }) {
  const s = d.slips
  return (
    <>
      <H id="revenue">รายได้ & สลิป</H>
      <div className="grid stats">
        <Stat label="รายได้ยืนยันแล้ว" value={baht(s.revenue_verified)} />
        <Stat label="สัปดาห์นี้" value={baht(s.revenue_week)} />
        <Stat label="รอตรวจ" value={nf(s.pending_action)} sub={`ใหม่ ${s.by_status.new} · ตรวจเอง ${s.by_status.review}`} />
        <Stat label="ยืนยันแล้ว" value={nf(s.by_status.verified)} sub={`ไม่ผ่าน ${s.by_status.rejected}`} />
        <Stat label="สลิปวันนี้" value={nf(s.today)} sub={`7 วัน ${nf(s.week)}`} />
        <Stat label="ทั้งหมด" value={nf(s.total)} />
      </div>

      <Card title="สลิปรายวัน" right={<button className="xs primary" onClick={() => nav('/slips')}>เปิดหน้าสลิป</button>}>
        <StackBars data={s.daily} keys={[
          { k: 'verified', name: 'ผ่าน', color: ST.verified },
          { k: 'review', name: 'ตรวจเอง', color: ST.review },
          { k: 'new', name: 'ใหม่', color: ST.new },
          { k: 'rejected', name: 'ไม่ผ่าน', color: ST.rejected },
        ]} height={230} />
      </Card>

      <div className="grid two">
        <Card title="แยกตามหลักสูตร" sub="ยอดที่ยืนยันแล้ว">
          <table>
            <thead><tr><th>หลักสูตร</th><th>สลิป</th><th>ยืนยัน</th><th>ยอดเงิน</th></tr></thead>
            <tbody>
              {(s.by_course || []).map((c) => (
                <tr key={c.course}><td><b>{c.course}</b></td><td>{nf(c.count)}</td><td>{nf(c.verified)}</td><td>{baht(Math.round(c.amount))}</td></tr>
              ))}
              {!s.by_course?.length && <tr><td colSpan={4} className="muted">ยังไม่มี</td></tr>}
            </tbody>
          </table>
        </Card>
        <Card title="สัดส่วนสถานะสลิป">
          <Donut rows={[
            { k: 'verified', name: 'ผ่าน', v: s.by_status.verified },
            { k: 'review', name: 'ต้องตรวจเอง', v: s.by_status.review },
            { k: 'new', name: 'ใหม่', v: s.by_status.new },
            { k: 'rejected', name: 'ไม่ผ่าน', v: s.by_status.rejected },
          ]} nameKey="name" valueKey="v" colors={(r) => ST[r.k]} />
        </Card>
      </div>

      <div className="grid two">
        <Card title="เส้นทางสู่การชำระเงิน"><Funnel steps={a?.funnel} /></Card>
        <Card title="สลิปล่าสุด">
          <ul className="loglist">
            {(d.recent_slips || []).map((sl) => (
              <li key={sl.id}>
                {sl.picture_url ? <img className="avatar-xs" src={sl.picture_url} alt="" /> : <span className="dot" />}
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {sl.display_name || (sl.line_user_id || '').slice(0, 10)}
                  {sl.expected_course && <span className="chip xs"> {sl.expected_course}</span>}
                </span>
                {sl.amount && <b>{baht(sl.amount)}</b>}
                <span className={`chip xs ${sl.status === 'verified' ? 'ok' : sl.status === 'rejected' ? 'failed' : ''}`}>{sl.status}</span>
              </li>
            ))}
            {!d.recent_slips?.length && <li className="muted">ยังไม่มี</li>}
          </ul>
        </Card>
      </div>
    </>
  )
}

/* ---------- 4. ข้อความ ---------- */
function Messages({ d, a }) {
  const dl = a?.delivery || {}
  return (
    <>
      <H id="messages">ข้อความ & แชท</H>
      <div className="grid stats">
        <Stat label="ข้อความเข้า 7 วัน" value={nf(d.messages.in_7d)} />
        <Stat label="ข้อความออก 7 วัน" value={nf(d.messages.out_7d)} />
        <Stat label="Webhook 7 วัน" value={nf(d.messages.webhook_7d)} sub={`วันนี้ ${nf(d.system.webhook_today)}`} />
        <Stat label="แชทค้างตอบ" value={nf(d.users.unread_threads)} />
      </div>

      <div className="grid two">
        <Card title="ข้อความเข้า / วัน">
          <TimeArea data={a?.messages?.in_daily} keys={[{ k: 'count', name: 'เข้า', color: CAT[0] }]} />
        </Card>
        <Card title="ข้อความออก / วัน">
          <TimeArea data={a?.messages?.out_daily} keys={[{ k: 'count', name: 'ออก', color: CAT[1] }]} />
        </Card>
      </div>

      <div className="grid two">
        <Card title="ชนิดข้อความที่เข้ามา">
          <BarList rows={a?.messages?.by_type} labelKey="type" valueKey="count" color={CAT} />
        </Card>
        <Card title="ข้อความออก — แยกที่มา" sub="อัตโนมัติ vs แอดมิน vs AI">
          <BarList rows={a?.messages?.out_by_source} labelKey="source" valueKey="count" color={CAT[1]} />
        </Card>
      </div>

      <Card title="ช่วงเวลาที่ลูกค้าทักเข้ามา" sub="วัน × ชั่วโมง (เข้ม = เยอะ)">
        <Heatmap grid={a?.messages?.heatmap} />
      </Card>

      <div className="grid two">
        <Card title="ลูกค้าที่ทักบ่อยสุด" sub={`${d.range_days} วัน`}>
          <PeopleList rows={a?.messages?.top_talkers} valueLabel="ครั้ง" />
        </Card>
        <Card title="Webhook แยกชนิด">
          <BarList rows={a?.webhook?.by_type} labelKey="type" valueKey="count" color={CAT} />
        </Card>
      </div>

      {(dl.broadcast != null || dl.apiPush != null) && (
        <Card title="การส่งข้อความ (LINE insight)" sub="เมื่อ 2 วันก่อน">
          <div className="chip-grid">
            {dl.broadcast != null && <span className="mchip">Broadcast <b>{nf(dl.broadcast)}</b></span>}
            {dl.targeting != null && <span className="mchip">Narrowcast <b>{nf(dl.targeting)}</b></span>}
            {dl.autoResponse != null && <span className="mchip">ตอบอัตโนมัติ <b>{nf(dl.autoResponse)}</b></span>}
            {dl.welcomeResponse != null && <span className="mchip">ต้อนรับ <b>{nf(dl.welcomeResponse)}</b></span>}
            {dl.chat != null && <span className="mchip">แชท <b>{nf(dl.chat)}</b></span>}
            {dl.apiBroadcast != null && <span className="mchip">API broadcast <b>{nf(dl.apiBroadcast)}</b></span>}
            {dl.apiPush != null && <span className="mchip">API push <b>{nf(dl.apiPush)}</b></span>}
            {dl.apiMulticast != null && <span className="mchip">API multicast <b>{nf(dl.apiMulticast)}</b></span>}
            {dl.apiReply != null && <span className="mchip">API reply <b>{nf(dl.apiReply)}</b></span>}
          </div>
        </Card>
      )}
    </>
  )
}

/* ---------- 5. บอท ---------- */
function Bot({ d, a }) {
  const runs = a?.bot?.automation_runs || {}
  return (
    <>
      <H id="bot">บอท & อัตโนมัติ</H>
      <div className="grid two">
        <Card title="กฎตอบอัตโนมัติ — ใช้บ่อยสุด">
          <table>
            <thead><tr><th>กฎ</th><th>trigger</th><th>ครั้ง</th></tr></thead>
            <tbody>
              {(a?.bot?.top_rules || []).map((r, i) => (
                <tr key={i} style={{ opacity: r.enabled ? 1 : 0.45 }}>
                  <td>{r.name || '(ไม่มีชื่อ)'}</td><td><span className="chip xs">{r.trigger}</span></td><td><b>{nf(r.hits)}</b></td>
                </tr>
              ))}
              {!a?.bot?.top_rules?.length && <tr><td colSpan={3} className="muted">ยังไม่มี</td></tr>}
            </tbody>
          </table>
        </Card>
        <Card title="Postback — ใช้บ่อยสุด">
          <table>
            <thead><tr><th>ปุ่ม</th><th>ครั้ง</th></tr></thead>
            <tbody>
              {(a?.bot?.top_postbacks || []).map((r, i) => (
                <tr key={i} style={{ opacity: r.enabled ? 1 : 0.45 }}>
                  <td title={r.data}>{r.label || r.data}</td><td><b>{nf(r.hits)}</b></td>
                </tr>
              ))}
              {!a?.bot?.top_postbacks?.length && <tr><td colSpan={2} className="muted">ยังไม่มี</td></tr>}
            </tbody>
          </table>
        </Card>
      </div>

      <div className="grid two">
        <Card title="Automation" sub={`รอส่ง ${nf(runs.pending || 0)} · สำเร็จ ${nf(runs.done || 0)} · ล้มเหลว ${nf(runs.failed || 0)} · ข้าม ${nf(runs.skipped || 0)}`}>
          <table>
            <thead><tr><th>ชื่อ</th><th>trigger</th><th>สถานะ</th><th>รัน</th></tr></thead>
            <tbody>
              {(a?.bot?.automations || []).map((r, i) => (
                <tr key={i}>
                  <td>{r.name}</td><td><span className="chip xs">{r.trigger}</span></td>
                  <td><span className={`chip xs ${r.enabled ? 'ok' : ''}`}>{r.enabled ? 'เปิด' : 'ปิด'}</span></td>
                  <td>{nf(r.runs)}</td>
                </tr>
              ))}
              {!a?.bot?.automations?.length && <tr><td colSpan={4} className="muted">ยังไม่มี</td></tr>}
            </tbody>
          </table>
        </Card>
        <Card title="งานตั้งเวลา (กำลังจะถึง)">
          <ul className="loglist">
            {(a?.scheduled || []).map((j, i) => (
              <li key={i}>
                <span className="dot" />{j.label || j.kind}
                {j.repeat && <span className="chip xs">{j.repeat}</span>}
                <span className="muted xs"> · {dtf(j.run_at)}</span>
              </li>
            ))}
            {!a?.scheduled?.length && <li className="muted">ไม่มีงานค้าง</li>}
          </ul>
        </Card>
      </div>

      <Card title="ลิงก์สั้น — คลิกรวม">
        <TimeArea data={a?.links?.daily} keys={[{ k: 'count', name: 'คลิก', color: CAT[5] }]} height={170} />
        <table style={{ marginTop: 8 }}>
          <thead><tr><th>โค้ด</th><th>ป้าย</th><th>คลิกรวม</th></tr></thead>
          <tbody>
            {(a?.links?.top || []).map((l) => (
              <tr key={l.code}><td><code>{l.code}</code></td><td>{l.label || '–'}</td><td><b>{nf(l.clicks)}</b></td></tr>
            ))}
            {!a?.links?.top?.length && <tr><td colSpan={3} className="muted">ยังไม่มีลิงก์</td></tr>}
          </tbody>
        </table>
      </Card>
    </>
  )
}

/* ---------- 6. ระบบ ---------- */
function System({ d, a }) {
  const sys = d.system, bot = d.bot || {}
  return (
    <>
      <H id="system">ระบบ</H>
      <div className="grid stats">
        <Stat label="โควตาข้อความ" value={sys.quota_pct != null ? sys.quota_pct + '%' : '–'} sub={`${nf(sys.quota_used)} / ${nf(sys.quota_limit)}`} />
        <Stat label="คาดใช้ทั้งเดือน" value={nf(sys.quota_projected)}
              sub={sys.quota_limit && sys.quota_projected > sys.quota_limit ? '⚠ เกินโควตา' : 'อยู่ในเกณฑ์'} />
        <Stat label="Error 24 ชม." value={nf(sys.errors_24h)} />
        <Stat label="งานค้าง" value={nf(sys.tasks_open)} sub={sys.tasks_overdue ? `⚠ เกินกำหนด ${sys.tasks_overdue}` : 'ไม่เกินกำหนด'} />
      </div>

      <Card title="โควตาข้อความเดือนนี้">
        <div className="quota-bar">
          <span className="quota-fill" style={{
            width: `${Math.min(sys.quota_pct || 0, 100)}%`,
            background: (sys.quota_pct || 0) > 85 ? ST.rejected : (sys.quota_pct || 0) > 60 ? ST.review : ST.verified,
          }} />
        </div>
        <div className="row spread muted xs" style={{ marginTop: 4 }}>
          <span>ใช้ {nf(sys.quota_used)}</span>
          <span>คาด {nf(sys.quota_projected)} / เพดาน {nf(sys.quota_limit)}</span>
        </div>
      </Card>

      <div className="grid two">
        <Card title="สถานะ Cron">
          <ul className="loglist">
            {(sys.cron || []).map((c, i) => (
              <li key={i}>
                <span className={`dot ${c.status === 'ok' ? 'ok' : c.status || ''}`} />
                <code>{c.job}</code><span className="muted xs"> · {dtf(c.last_run)}</span>
              </li>
            ))}
            {!sys.cron?.length && <li className="muted">ยังไม่มี log</li>}
          </ul>
        </Card>
        <Card title="ขนาดข้อมูล">
          <BarList rows={Object.entries(sys.db_rows || {}).map(([k, v]) => ({ k, v }))} labelKey="k" valueKey="v" color={CAT[7]} />
        </Card>
      </div>

      <Card title="Webhook events / วัน">
        <TimeArea data={a?.webhook?.daily} keys={[{ k: 'count', name: 'events', color: CAT[4] }]} height={170} />
      </Card>

      <div className="grid two">
        <Card title="ปฏิบัติการล่าสุด"><OpsList ops={d.recent_operations} /></Card>
        <Card title="การส่งข้อความล่าสุด">
          <table>
            <thead><tr><th>ชนิด</th><th>ปลายทาง</th><th>สถานะ</th><th>เวลา</th></tr></thead>
            <tbody>
              {(d.recent_broadcasts || []).map((b) => (
                <tr key={b.id}>
                  <td>{b.kind}</td><td>{nf(b.target_count)}</td>
                  <td><span className={`chip ${b.status}`}>{b.status}</span></td>
                  <td className="muted xs">{dtf(b.created_at)}</td>
                </tr>
              ))}
              {!d.recent_broadcasts?.length && <tr><td colSpan={4} className="muted">ยังไม่มี</td></tr>}
            </tbody>
          </table>
        </Card>
      </div>

      {bot.displayName && (
        <Card title="ข้อมูลบอท">
          <div className="row" style={{ gap: 12 }}>
            {bot.pictureUrl && <img className="avatar" src={bot.pictureUrl} alt="" />}
            <div>
              <div><b>{bot.displayName}</b></div>
              <div className="muted sm">{bot.basicId} {bot.premiumId ? `· ${bot.premiumId}` : '· unverified'}</div>
              {bot.chatMode && <div className="muted xs">chatMode: {bot.chatMode} · markAsRead: {bot.markAsReadMode}</div>}
            </div>
          </div>
        </Card>
      )}
    </>
  )
}

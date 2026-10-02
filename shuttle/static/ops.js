// 运营调度台
let trips = [], vehicles = [], detail = null, records = [];

async function loadBase() {
  [trips, vehicles] = await Promise.all([api("/api/trips"), api("/api/vehicles")]);
  const sel = $("#tripSelect");
  const prev = sel.value;
  sel.innerHTML = trips.map((t) =>
    `<option value="${t.id}">${esc(t.name)}（${esc(t.plate)} · ${t.seats}座）</option>`).join("");
  if (prev && trips.some((t) => t.id === prev)) sel.value = prev;

  $("#vehicleSel").innerHTML = vehicles
    .filter((v) => v.id !== trips.find((t) => t.id === sel.value)?.vehicle_id)
    .map((v) => `<option value="${v.id}">${esc(v.plate)}（${v.seats} 座）</option>`).join("");
}

async function loadDetail() {
  const tid = $("#tripSelect").value;
  detail = await api(`/api/trips/${tid}`);
  records = await api(`/api/records?trip=${tid}`);

  const c = detail.counts;
  $("#statusLine").innerHTML = `
    ${badge(detail.status)}
    车辆 <b>${esc(detail.plate)}</b>（${detail.seats} 座）·
    预订 ${c.booked} · 车上 ${c.boarded} · 已完成 ${c.alighted} ·
    候补 ${c.waitlisted} · 爽约 ${c.no_show} · 取消 ${c.cancelled}
    <span class="muted">| 当前：${esc(detail.location.next === "—" ? "—" : detail.location.next)}
      （${detail.location.state}）</span>`;

  // 区间容量条
  $("#segBars").innerHTML = detail.segments
    .filter((s) => !s.from_skipped && !s.to_skipped)
    .map((s) => {
      const ratio = Math.round((s.load / s.seats) * 100);
      const full = s.free === 0;
      return `<span class="seg-chip ${full ? "zero" : ""}">
        ${esc(s.from)}→${esc(s.to)} <b>${s.load}/${s.seats}</b>
        <span class="capbar ${full ? "full" : ""}" style="width:80px">
          <i style="width:${ratio}%"></i></span>
      </span>`;
    }).join("");

  renderReservations();
  renderRecords();
  renderDetour();
  renderDrivers();

  const at = detail.location.state === "at";
  $("#arriveBtn").disabled = detail.status === "completed" || at;
  $("#departBtn").disabled = detail.status === "completed" || !at;
  $("#detourBtn").disabled = detail.status !== "scheduled";
}

function sn(seq) { return detail.stops.find((s) => s.seq === seq)?.name || `#${seq}`; }

function renderReservations() {
  const rows = detail.reservations;
  $("#resTable").innerHTML = `<table><thead><tr>
    <th>#</th><th>员工</th><th>区间</th><th>状态</th><th>票号</th><th></th>
  </tr></thead><tbody>${rows.map((r) => `<tr>
    <td>${r.id}</td>
    <td>${esc(r.employee_name)}</td>
    <td>${esc(sn(r.board_seq))} → ${esc(sn(r.alight_seq))}
        ${r.remap_flag ? '<span class="badge waitlisted">待确认</span>' : ""}</td>
    <td>${badge(r.status)}</td>
    <td class="tkt" style="font-family:monospace;font-size:12px">${esc(r.ticket)}</td>
    <td>${["booked", "waitlisted", "boarded"].includes(r.status)
        ? `<button class="small ghost" data-cancel="${r.id}">取消</button>` : ""}</td>
  </tr>`).join("")}</tbody></table>`;
  $$("#resTable button[data-cancel]").forEach((b) =>
    b.onclick = async () => {
      if (!confirm("运营员代为取消该预约？")) return;
      try {
        const r = await api(`/api/reservations/${b.dataset.cancel}/cancel`,
          { method: "POST", body: { reason: "运营员调度取消" } });
        toast(`已取消，候补递补 ${r.promoted.length} 人`, "ok");
        await loadDetail();
      } catch (e) { toast(e.message, "err"); }
    });
}

function renderRecords() {
  if (!records.length) {
    $("#recordsTable").innerHTML = '<p class="muted">暂无扫码记录。</p>';
    return;
  }
  $("#recordsTable").innerHTML = `<table><thead><tr>
    <th>员工</th><th>实际上车</th><th>实际下车</th><th>结果</th>
  </tr></thead><tbody>${records.map((r) => `<tr>
    <td>${esc(r.employee_name)}<div class="muted" style="font-size:12px">${esc(r.ticket)}</div></td>
    <td>${r.real_board_stop ? esc(r.real_board_stop) : "—"}
        ${r.boarded_at ? `<div class="muted" style="font-size:12px">${esc(r.boarded_at.slice(5, 16).replace("T", " "))}</div>` : ""}</td>
    <td>${r.real_alight_stop ? esc(r.real_alight_stop) : "—"}
        ${r.alighted_at ? `<div class="muted" style="font-size:12px">${esc(r.alighted_at.slice(5, 16).replace("T", " "))}</div>` : ""}</td>
    <td>${badge(r.status)}</td>
  </tr>`).join("")}</tbody></table>`;
}

function renderDetour() {
  $("#skipStops").innerHTML = detail.stops.map((s) =>
    `<label style="display:flex;align-items:center;gap:8px;margin:3px 0;color:#1c2733">
      <input type="checkbox" value="${s.seq}" ${s.skipped ? "checked" : ""}>
      ${s.seq}. ${esc(s.name)} ${s.skipped ? '<span class="muted">(已跳过)</span>' : ""}
    </label>`).join("");
}

function renderDrivers() {
  $("#drivers").innerHTML = detail.drivers.map((d) => `
    <div class="pick-card">
      <div style="flex:1">
        <div class="who">${esc(d.name)}</div>
        <div class="muted">${d.checked_at
          ? `已于 ${esc(d.checked_at.replace("T", " "))} 核销（${esc(d.checked_by)}）`
          : "未核销"}</div>
      </div>
      ${d.checked_at ? '<span class="badge booked">已核销</span>'
        : `<button class="small green" data-driver="${esc(d.id)}">核销出勤</button>`}
    </div>`).join("");
  $$("#drivers button[data-driver]").forEach((b) =>
    b.onclick = async () => {
      try {
        await api(`/api/trips/${detail.id}/attendance`,
          { method: "POST", body: { driver_id: b.dataset.driver } });
        toast("出勤已核销并通知司机", "ok");
        await loadDetail();
      } catch (e) {
        toast(e.code === "forbidden" ? "权限拒绝：您未被授权该线路" : e.message, "err");
      }
    });
}

$("#swapBtn").onclick = async () => {
  const vid = $("#vehicleSel").value;
  if (!confirm(`确认更换为 ${$("#vehicleSel").selectedOptions[0].textContent}？缩容将把多余乘客降为优先候补。`)) return;
  try {
    const r = await api(`/api/trips/${detail.id}/vehicle`,
      { method: "POST", body: { vehicle_id: vid } });
    toast(`换车完成：降补 ${r.demoted.length} 人，递补 ${r.promoted.length} 人`, "ok", 4500);
    await loadBase();
    await loadDetail();
  } catch (e) {
    toast(e.code === "forbidden" ? "权限拒绝：您未被授权该线路" : e.message, "err");
  }
};

$("#detourBtn").onclick = async () => {
  const seqs = $$("#skipStops input:checked").map((i) => +i.value);
  try {
    const r = await api(`/api/trips/${detail.id}/detour`,
      { method: "POST", body: { seqs } });
    toast(`绕行已应用：改站 ${r.affected.length} 人，取消 ${r.cancelled.length} 人，递补 ${r.promoted.length} 人`,
      "ok", 4500);
    await loadDetail();
  } catch (e) {
    toast(e.code === "forbidden" ? "权限拒绝：您未被授权该线路" : e.message, "err");
  }
};

$("#arriveBtn").onclick = async () => {
  try { await api(`/api/trips/${detail.id}/advance`, { method: "POST" }); await loadDetail(); }
  catch (e) { toast(e.message, "err"); }
};
$("#departBtn").onclick = async () => {
  try {
    const r = await api(`/api/trips/${detail.id}/advance`, { method: "POST" });
    if (r.event.no_show.length) toast(`${r.event.no_show.length} 人爽约，候补递补 ${r.event.promoted.length} 人`, "info", 4500);
    await loadDetail();
  } catch (e) { toast(e.message, "err"); }
};

$("#resetBtn").onclick = async () => {
  if (!confirm("重置全部演示数据？")) return;
  await api("/api/seed", { method: "POST", body: {} });
  toast("演示数据已重置", "ok");
  await loadBase();
  await loadDetail();
};
$("#tripSelect").addEventListener("change", loadDetail);

(async function init() {
  await renderTopbar("ops");
  await loadBase();
  await loadDetail();
})();

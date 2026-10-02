// 司机车载端
let trips = [];
let detail = null;

async function loadTrips() {
  const all = await api("/api/trips");
  // 只列出自己担任司机的班次；其他班次点开会被服务端拒绝
  const me = getUid();
  trips = all;
  const sel = $("#tripSelect");
  const prev = sel.value;
  sel.innerHTML = all.map((t) =>
    `<option value="${t.id}">${esc(t.name)}（${esc(t.plate)}）— ${STATUS_CN[t.status]}</option>`).join("");
  if (prev && all.some((t) => t.id === prev)) sel.value = prev;
}

async function loadDetail() {
  const tid = $("#tripSelect").value;
  detail = await api(`/api/trips/${tid}`);

  const loc = detail.location;
  $("#stateLabel").textContent =
    detail.status === "completed" ? "已收车"
    : loc.state === "at" ? "正在停靠" : "下一站";
  $("#nextStop").textContent = detail.status === "completed"
    ? "—" : (loc.next && loc.next !== "—" ? loc.next : "终点站");
  $("#plateLine").textContent = `${detail.plate} · ${detail.seats}座 · ${esc(detail.route_name)}`;
  $("#phaseHint").textContent = loc.state === "at"
    ? `已到达 ${loc.stop}，请核验上车` : loc.state === "between"
    ? `已离开 ${loc.stop}` : "车辆待发车";

  $("#arriveBtn").disabled = detail.status !== "scheduled" || loc.state === "at";
  $("#departBtn").disabled = detail.status === "completed" || loc.state !== "at";
  if (detail.status === "active" && loc.state !== "at") $("#arriveBtn").disabled = false;

  renderPickList();
  renderOnboard();
  renderAttendance();
}

function sname(seq) { return detail.stops.find((s) => s.seq === seq)?.name || `#${seq}`; }

function renderPickList() {
  const cur = detail.pos_seq;
  const atStop = detail.location.state === "at";
  const picks = detail.reservations.filter((r) => r.status === "booked");
  $("#pickHint").textContent = picks.length ? `共 ${picks.length} 人待核验` : "暂无";
  if (!picks.length) {
    $("#pickList").innerHTML = '<p class="muted">没有待上车人员。</p>';
    return;
  }
  $("#pickList").innerHTML = picks.map((r) => {
    const boardingNow = atStop && r.board_seq === cur;
    return `<div class="pick-card" style="${boardingNow ? "border-left-width:6px" : ""}">
      <div style="flex:1">
        <div class="who" style="${boardingNow ? "color:var(--green)" : ""}">
          ${esc(r.employee_name)} ${boardingNow ? "— 本站上车" : ""}</div>
        <div class="muted">${esc(sname(r.board_seq))} 上 → ${esc(sname(r.alight_seq))} 下</div>
        <div class="tkt">${esc(r.ticket)}</div>
      </div>
      <button class="small green" data-ticket="${esc(r.ticket)}">模拟扫码上车</button>
    </div>`;
  }).join("");
  $$("#pickList button[data-ticket]").forEach((b) =>
    b.addEventListener("click", () => doScan("board", b.dataset.ticket)));
}

function renderOnboard() {
  const onboard = detail.reservations.filter((r) => r.status === "boarded");
  if (!onboard.length) {
    $("#onboard").innerHTML = '<p class="muted">车上暂无已核验乘客。</p>';
    return;
  }
  $("#onboard").innerHTML = `<table><thead><tr>
      <th>员工</th><th>上车站</th><th>下车站</th><th>票号</th><th></th></tr></thead><tbody>
    ${onboard.map((r) => `<tr>
      <td>${esc(r.employee_name)}</td>
      <td>${esc(sname(r.board_seq))}</td>
      <td>${esc(sname(r.alight_seq))}</td>
      <td class="tkt">${esc(r.ticket)}</td>
      <td><button class="small" data-aticket="${esc(r.ticket)}">扫码下车</button></td>
    </tr>`).join("")}</tbody></table>`;
  $$("#onboard button[data-aticket]").forEach((b) =>
    b.addEventListener("click", () => doScan("alight", b.dataset.aticket)));
}

function renderAttendance() {
  const me = getUid();
  const mine = detail.drivers.find((d) => d.id === me);
  if (!mine) {
    $("#attendanceBox").innerHTML =
      '<p class="muted">您不是该班次司机，无法在此班次核验。</p>';
    return;
  }
  $("#attendanceBox").innerHTML = mine.checked_at
    ? `✅ 出勤已于 <b>${esc(mine.checked_at.replace("T", " "))}</b> 由运营员核销`
    : "⏳ 出勤待运营员核销（司机不可自行核销）";
}

async function doScan(kind, ticket) {
  $("#ticketInput").value = ticket;
  const action = kind === "board" ? "scan-board" : "scan-alight";
  const box = $("#scanResult");
  try {
    const r = await api(`/api/trips/${$("#tripSelect").value}/${action}`, {
      method: "POST", body: { ticket },
    });
    box.className = `scan-result ${r.idempotent ? "dup" : "ok"}`;
    box.textContent = (r.idempotent ? "♻ 幂等：" : "✓ ") + r.message;
    toast(r.message, r.idempotent ? "info" : "ok");
    await loadDetail();
  } catch (e) {
    box.className = "scan-result err";
    box.textContent = "✕ " + e.message;
    toast(e.message, "err");
  }
}

$("#boardScanBtn").onclick = () => doScan("board", $("#ticketInput").value.trim());
$("#alightScanBtn").onclick = () => doScan("alight", $("#ticketInput").value.trim());

$("#arriveBtn").onclick = async () => {
  try {
    const r = await api(`/api/trips/${$("#tripSelect").value}/advance`, { method: "POST" });
    toast(`已到达 ${detail.stops.find((s) => s.seq === r.event.at)?.name || ""}`, "ok");
    await loadDetail();
  } catch (e) { toast(e.message, "err"); }
};

$("#departBtn").onclick = async () => {
  try {
    const r = await api(`/api/trips/${$("#tripSelect").value}/advance`, { method: "POST" });
    const ev = r.event;
    let msg = `已离站`;
    if (ev.no_show?.length) msg += `；${ev.no_show.length} 人未上车判爽约`;
    if (ev.promoted?.length) msg += `；候补递补 ${ev.promoted.length} 人`;
    if (ev.completed) msg = "班次已结束（全部站点完成）";
    toast(msg, ev.no_show?.length ? "info" : "ok", 4500);
    await loadDetail();
  } catch (e) { toast(e.message, "err"); }
};

$("#tripSelect").addEventListener("change", loadDetail);

(async function init() {
  await renderTopbar("driver");
  await loadTrips();
  // 默认选中自己驾驶的第一个班次
  const mine = trips[0];
  await loadDetail();
})();

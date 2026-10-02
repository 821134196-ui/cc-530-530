// 员工预订页逻辑
let trips = [];
let detail = null;

async function loadTrips() {
  trips = await api("/api/trips");
  const sel = $("#tripSelect");
  const prev = sel.value;
  sel.innerHTML = trips
    .map((t) => `<option value="${t.id}">${esc(t.name)}（${esc(t.plate)} · ${t.seats}座 · ${STATUS_CN[t.status]}）</option>`)
    .join("");
  if (prev && trips.some((t) => t.id === prev)) sel.value = prev;
}

async function loadDetail() {
  const tid = $("#tripSelect").value;
  detail = await api(`/api/trips/${tid}?viewer=${getUid()}`);

  $("#tripMeta").innerHTML =
    `${esc(detail.plate)} · ${detail.seats} 座 ${badge(detail.status)}
     <div class="muted" style="margin-top:4px">${esc(detail.route_name)}</div>`;

  // 顺序图：若本人有预约则高亮上/下车站
  const my = detail.my_reservation;
  $("#diagram").innerHTML = renderStopDiagram(detail.stops, {
    board: my ? my.board_seq : null,
    alight: my ? my.alight_seq : null,
    segments: detail.segments,
  });
  $("#diagramHint").innerHTML = my
    ? `绿色为您的乘车站点：${esc(detail.stops[my.board_seq].name)} 上车 →
       ${esc(detail.stops[my.alight_seq].name)} 下车。`
    : "绿色条表示已占用，每条边显示该区间剩余座位。";

  const pick = renderStopPicker(detail.stops);
  $("#boardSel").innerHTML = pick;
  $("#alightSel").innerHTML = pick;
  if (my) {
    $("#boardSel").value = my.board_seq;
    $("#alightSel").value = my.alight_seq;
  } else {
    $("#alightSel").value = 1;
  }

  renderMy(my);
  renderWaitlist();
  await loadInbox();
}

function renderMy(my) {
  if (!my) {
    $("#myBox").innerHTML = '<p class="muted">您在该班次暂无预约。</p>';
    $("#bookBtn").textContent = "提交预约";
    $("#bookBtn").disabled = false;
    return;
  }
  const remap = my.remap_flag
    ? `<div style="color:var(--amber);font-weight:700;margin:6px 0">
         ⚠ 绕行后您的${my.remap_flag === "board" ? "上车站" : "下车站"}已调整，请确认
       </div>` : "";
  $("#myBox").innerHTML = `
    ${remap}
    <div class="pick-card">
      <div style="flex:1">
        <div class="who">${esc(detail.stops[my.board_seq].name)}
          <span class="muted">→</span> ${esc(detail.stops[my.alight_seq].name)}</div>
        <div class="tkt">票号 ${esc(my.ticket)} ${badge(my.status)}</div>
      </div>
      <button class="small green" id="changeBtn">按所选站点改站</button>
      <button class="small ghost" id="ackBtn" style="${my.remap_flag ? "" : "display:none"}">确认调整</button>
      <button class="small" id="cancelBtn">取消预约</button>
    </div>`;
  $("#bookBtn").textContent = "我已有预约（可用下方改站）";
  $("#bookBtn").disabled = true;
  $("#changeBtn").onclick = async () => {
    try {
      const r = await api(`/api/reservations/${my.id}/change`, {
        method: "POST",
        body: { board_seq: +$("#boardSel").value, alight_seq: +$("#alightSel").value },
      });
      toast(r.waitlisted ? "新区间已满，已转入候补队列" : "改站成功", "ok");
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  };
  const ackBtn = $("#ackBtn");
  if (ackBtn) ackBtn.onclick = async () => {
    try {
      await api(`/api/reservations/${my.id}/ack`, { method: "POST", body: {} });
      toast("已确认绕行后的站点调整", "ok");
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  };
  $("#cancelBtn").onclick = async () => {
    if (!confirm("确认取消该预约？")) return;
    try {
      const r = await api(`/api/reservations/${my.id}/cancel`, { method: "POST" });
      toast(`已取消${r.promoted.length ? `，候补递补 ${r.promoted.length} 人` : ""}`, "ok");
      await refresh();
    } catch (e) { toast(e.message, "err"); }
  };
}

function renderWaitlist() {
  if (!detail.waitlist.length) {
    $("#waitlist").innerHTML = '<p class="muted">候补队列空</p>';
    return;
  }
  $("#waitlist").innerHTML = detail.waitlist
    .map((r, i) => `<div class="inbox-item">
      #${i + 1} ${esc(r.employee_name)}
      <span class="muted">${esc(detail.stops[r.board_seq].name)}→${esc(detail.stops[r.alight_seq].name)}</span>
      ${r.waitlist_priority ? '<span class="badge neutral">优先</span>' : ""}
    </div>`).join("");
}

async function loadInbox() {
  const msgs = await api(`/api/inbox?uid=${getUid()}`);
  $("#inbox").innerHTML = msgs.length
    ? msgs.slice(0, 8).map((m) =>
        `<div class="inbox-item"><span class="t">${esc(m.title)}</span>
         <span class="ts">${esc(m.ts.slice(5, 16).replace("T", " "))}</span>
         <div>${esc(m.body)}</div></div>`).join("")
    : '<p class="muted">暂无通知</p>';
}

async function refresh() {
  await loadTrips();
  await loadDetail();
}

$("#tripSelect").addEventListener("change", loadDetail);
$("#boardSel").addEventListener("change", () => {
  if (+$("#alightSel").value <= +$("#boardSel").value) {
    $("#alightSel").value = Math.min(+$("#boardSel").value + 1, detail.stops.length - 1);
  }
});

$("#bookBtn").onclick = async () => {
  try {
    const r = await api(`/api/trips/${$("#tripSelect").value}/book`, {
      method: "POST",
      body: { board_seq: +$("#boardSel").value, alight_seq: +$("#alightSel").value },
    });
    toast(r.waitlisted ? "该区间已满，已进入候补队列" : "预约成功！", r.waitlisted ? "info" : "ok");
    await refresh();
  } catch (e) { toast(e.message, "err"); }
};

(async function init() {
  await renderTopbar("book");
  await refresh();
})();

// 公共工具：身份切换、API、Toast
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const getUid = () => localStorage.getItem("uid") || "e1";
const setUid = (uid) => localStorage.setItem("uid", uid);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": getUid(),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || "请求失败");
    err.code = data.error;
    throw err;
  }
  return data;
}

function toast(msg, kind = "info", ms = 3200) {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.className = `toast ${kind}`;
  el.textContent = msg;
  el.style.display = "block";
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.style.display = "none"), ms);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const STATUS_CN = {
  booked: "已预订", waitlisted: "候补中", boarded: "已上车",
  alighted: "已下车", no_show: "爽约", cancelled: "已取消",
  scheduled: "待发车", active: "行驶中", completed: "已结束",
};

function badge(status) {
  return `<span class="badge ${esc(status)}">${STATUS_CN[status] || esc(status)}</span>`;
}

// 顶栏（含身份切换）。page: book/driver/ops
async function renderTopbar(page) {
  const users = await api("/api/users");
  const groups = { employee: "员工", driver: "司机", operator: "运营员" };
  let opts = "";
  for (const role of ["employee", "driver", "operator"]) {
    opts += `<optgroup label="${groups[role]}">`;
    for (const u of users.filter((x) => x.role === role)) {
      opts += `<option value="${u.id}" ${u.id === getUid() ? "selected" : ""}>${esc(u.name)}（${u.id}）</option>`;
    }
    opts += "</optgroup>";
  }
  const links = [
    ["book", "员工预订", "/book.html"],
    ["driver", "司机核验", "/driver.html"],
    ["ops", "运营调度", "/ops.html"],
  ];
  document.body.insertAdjacentHTML("afterbegin", `
    <header class="topbar">
      <span class="brand">🚌 企业通勤班车</span>
      <nav>${links.map(([k, n, href]) =>
        `<a href="${href}" class="${k === page ? "active" : ""}">${n}</a>`).join("")}
        <a href="/">线路图</a>
      </nav>
      <span class="spacer"></span>
      <label style="color:#cfe0f2;margin:0">当前身份</label>
      <select id="uidSelect">${opts}</select>
    </header>`);
  $("#uidSelect").addEventListener("change", (e) => {
    setUid(e.target.value);
    location.reload();
  });
}

// 站点顺序图：stops=[{seq,name,skipped}]，board/alight 为 seq 高亮，
// segments 可选：在边下显示余座与容量条
function renderStopDiagram(stops, { board = null, alight = null, segments = null } = {}) {
  let html = '<ul class="stops">';
  stops.forEach((s, i) => {
    const cls = ["stop-item",
      s.skipped ? "skipped" : "",
      s.seq === board ? "is-board" : "",
      s.seq === alight ? "is-alight" : "",
    ].filter(Boolean).join(" ");
    let tag = "";
    if (s.seq === board) tag += '<span class="tag">上车</span>';
    if (s.seq === alight) tag += '<span class="tag alight">下车</span>';
    html += `<li class="${cls}"><span class="dot"></span>
      <span class="sname">${esc(s.name)}</span>${tag}`;
    if (segments && i < segments.length) {
      const seg = segments[i];
      const ratio = seg.seats ? Math.round((seg.load / seg.seats) * 100) : 0;
      const full = seg.free === 0;
      html += `<div class="edge-info">
        → ${esc(seg.to)}：余 <b class="${full ? "free-note zero" : ""}">${seg.free}</b>/${seg.seats}
        <span class="capbar ${full ? "full" : ""}"><i style="width:${ratio}%"></i></span>
      </div>`;
    }
    html += "</li>";
  });
  return html + "</ul>";
}

// 可交互的上/下车站选择（用于预订表单）
function renderStopPicker(stops, { includeSkipped = false } = {}) {
  return stops
    .filter((s) => includeSkipped || !s.skipped)
    .map((s) => `<option value="${s.seq}">${s.seq}. ${esc(s.name)}</option>`)
    .join("");
}

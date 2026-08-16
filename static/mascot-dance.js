// Self-contained SVG sprites and lifecycle for the keyboard dance easter egg.
function partnerDanceSprite() {
  return `<svg class="dance-sprite partner-sprite" viewBox="0 0 140 180" aria-hidden="true">
    <defs>
      <linearGradient id="partner-head" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#e8ff9d"/><stop offset=".55" stop-color="#bfe866"/><stop offset="1" stop-color="#83b94d"/></linearGradient>
      <linearGradient id="partner-body" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#b9e967"/><stop offset="1" stop-color="#719e45"/></linearGradient>
      <linearGradient id="partner-shoe" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#efffc0"/><stop offset="1" stop-color="#94bd55"/></linearGradient>
    </defs>
    <ellipse class="sprite-shadow" cx="70" cy="169" rx="46" ry="8"/>
    <g class="sprite-leg partner-leg-left"><rect x="48" y="119" width="17" height="38" rx="8" fill="url(#partner-body)" stroke="#254432" stroke-width="5"/><circle cx="56" cy="123" r="7" fill="#d9f886" stroke="#254432" stroke-width="4"/><path d="M38 151c8-6 22-5 28 2l-1 10H37c-5 0-7-7 1-12Z" fill="url(#partner-shoe)" stroke="#254432" stroke-width="5"/></g>
    <g class="sprite-leg partner-leg-right"><rect x="76" y="119" width="17" height="38" rx="8" fill="url(#partner-body)" stroke="#254432" stroke-width="5"/><circle cx="84" cy="123" r="7" fill="#d9f886" stroke="#254432" stroke-width="4"/><path d="M77 153c8-7 23-7 30 0 7 7 4 11-2 11H77Z" fill="url(#partner-shoe)" stroke="#254432" stroke-width="5"/></g>
    <g class="sprite-body partner-body"><path d="M45 75c6-9 44-9 50 0l8 45c-13 12-53 12-66 0Z" fill="url(#partner-body)" stroke="#254432" stroke-width="6"/><path d="M53 85c9 5 25 5 34 0" fill="none" stroke="#eaffaa" stroke-width="4" stroke-linecap="round" opacity=".75"/><path d="m70 91 5 9 10 2-7 7 2 10-10-5-10 5 2-10-7-7 10-2Z" fill="#ecffad" stroke="#476b38" stroke-width="3"/></g>
    <g class="sprite-arm partner-arm-left"><path d="M47 79c-15 1-25 12-30 26" fill="none" stroke="#254432" stroke-width="17" stroke-linecap="round"/><path d="M47 79c-15 2-23 12-29 27" fill="none" stroke="#a9da5e" stroke-width="10" stroke-linecap="round"/><circle cx="16" cy="109" r="11" fill="#dafa85" stroke="#254432" stroke-width="5"/><path d="M11 105c4-3 9-3 13 0" fill="none" stroke="#f5ffc7" stroke-width="3" stroke-linecap="round"/></g>
    <g class="sprite-arm partner-arm-right"><path d="M93 79c15 1 25 12 30 26" fill="none" stroke="#254432" stroke-width="17" stroke-linecap="round"/><path d="M93 79c15 2 23 12 29 27" fill="none" stroke="#a9da5e" stroke-width="10" stroke-linecap="round"/><circle cx="124" cy="109" r="11" fill="#dafa85" stroke="#254432" stroke-width="5"/><path d="M117 105c4-3 9-3 13 0" fill="none" stroke="#f5ffc7" stroke-width="3" stroke-linecap="round"/></g>
    <g class="sprite-head partner-head"><path d="M68 18V8" fill="none" stroke="#31513a" stroke-width="5" stroke-linecap="round"/><circle cx="68" cy="6" r="7" fill="#e7ff99" stroke="#31513a" stroke-width="4"/><rect x="28" y="18" width="82" height="68" rx="25" fill="url(#partner-head)" stroke="#254432" stroke-width="6"/><path d="M39 33c16-13 43-15 60-3" fill="none" stroke="#f3ffc5" stroke-width="7" stroke-linecap="round" opacity=".65"/><g class="partner-eyes"><ellipse cx="52" cy="51" rx="8" ry="11" fill="#183127"/><ellipse cx="86" cy="51" rx="8" ry="11" fill="#183127"/><circle cx="49" cy="47" r="2.5" fill="#f5ffd4"/><circle cx="83" cy="47" r="2.5" fill="#f5ffd4"/></g><path d="M51 66c9 9 27 9 36 0" fill="none" stroke="#183127" stroke-width="5" stroke-linecap="round"/><circle cx="43" cy="63" r="4" fill="#f5b9a7" opacity=".65"/><circle cx="96" cy="63" r="4" fill="#f5b9a7" opacity=".65"/></g>
  </svg>`;
}

function scoutDanceSprite() {
  return `<svg class="dance-sprite scout-sprite" viewBox="0 0 140 180" aria-hidden="true">
    <defs>
      <linearGradient id="scout-armor" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#84905c"/><stop offset=".5" stop-color="#566343"/><stop offset="1" stop-color="#303b31"/></linearGradient>
      <linearGradient id="scout-brass" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#f2c65c"/><stop offset="1" stop-color="#9a6d26"/></linearGradient>
      <radialGradient id="scout-eye"><stop stop-color="#e8ffc7"/><stop offset=".35" stop-color="#9cff76"/><stop offset="1" stop-color="#3b7e3d"/></radialGradient>
    </defs>
    <ellipse class="sprite-shadow" cx="70" cy="169" rx="47" ry="8"/>
    <g class="sprite-leg scout-leg-left"><path d="M47 118h20l-3 39H44Z" fill="url(#scout-armor)" stroke="#2a332b" stroke-width="6"/><circle cx="57" cy="122" r="7" fill="#d5a443" stroke="#2a332b" stroke-width="4"/><path d="M38 151h28l4 12H35c-6 0-5-8 3-12Z" fill="url(#scout-brass)" stroke="#2a332b" stroke-width="5"/><path d="M43 154h18" stroke="#f4d17b" stroke-width="3" opacity=".6"/></g>
    <g class="sprite-leg scout-leg-right"><path d="M74 118h20l3 39H77Z" fill="url(#scout-armor)" stroke="#2a332b" stroke-width="6"/><circle cx="84" cy="122" r="7" fill="#d5a443" stroke="#2a332b" stroke-width="4"/><path d="M75 151h28c8 4 9 12 3 12H72Z" fill="url(#scout-brass)" stroke="#2a332b" stroke-width="5"/><path d="M80 154h18" stroke="#f4d17b" stroke-width="3" opacity=".6"/></g>
    <g class="sprite-body scout-body"><path d="M40 72 52 64h39l11 10-5 48-12 10H54l-12-10Z" fill="url(#scout-armor)" stroke="#2a332b" stroke-width="6"/><path d="M51 82h40v31H51Z" fill="#1b2924" stroke="#d3a443" stroke-width="4"/><circle cx="71" cy="97" r="10" fill="#233a2d" stroke="#d3a443" stroke-width="4"/><circle class="scout-core" cx="71" cy="97" r="5" fill="#9cff76"/><path d="M55 118h32" stroke="#d3a443" stroke-width="4" stroke-linecap="round"/><circle cx="48" cy="75" r="3" fill="#f4d17b"/><circle cx="95" cy="75" r="3" fill="#f4d17b"/></g>
    <g class="sprite-arm scout-arm-left"><path d="M43 78 24 91 15 111" fill="none" stroke="#29322b" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/><path d="M43 78 25 92 16 111" fill="none" stroke="#65734c" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/><circle cx="25" cy="91" r="7" fill="#d2a142" stroke="#29322b" stroke-width="4"/><path d="m14 103-8 8 7 12 12-4 1-12Z" fill="url(#scout-brass)" stroke="#29322b" stroke-width="5"/></g>
    <g class="sprite-arm scout-arm-right"><path d="m99 78 18 14 10 19" fill="none" stroke="#29322b" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/><path d="m99 78 17 15 10 18" fill="none" stroke="#65734c" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/><circle cx="116" cy="92" r="7" fill="#d2a142" stroke="#29322b" stroke-width="4"/><path d="m126 103 9 8-7 12-12-4-1-12Z" fill="url(#scout-brass)" stroke="#29322b" stroke-width="5"/></g>
    <g class="sprite-head scout-head"><path d="m77 19 6-13" stroke="#d4a542" stroke-width="5" stroke-linecap="round"/><circle class="scout-signal" cx="84" cy="5" r="6" fill="#9cff76" stroke="#29322b" stroke-width="4"/><path d="m31 29 15-14h55l11 17-6 43-14 12H43L28 73Z" fill="url(#scout-armor)" stroke="#29322b" stroke-width="6"/><path d="m36 35 13-11h45" fill="none" stroke="#d7b35c" stroke-width="5" stroke-linecap="round" opacity=".75"/><path d="M39 39h58l5 27-10 10H45L35 65Z" fill="#12231d" stroke="#d1a343" stroke-width="4"/><g class="scout-eyes"><rect x="46" y="46" width="17" height="17" rx="4" fill="url(#scout-eye)" stroke="#8fcf69" stroke-width="3"/><rect x="75" y="46" width="17" height="17" rx="4" fill="url(#scout-eye)" stroke="#8fcf69" stroke-width="3"/></g><path class="scout-scan" d="M41 41h55" stroke="#c8ff9d" stroke-width="2" opacity=".75"/><path d="M52 70h34" stroke="#d1a343" stroke-width="4" stroke-linecap="round" stroke-dasharray="3 5"/><path d="M105 38h11v25h-12" fill="#4b573e" stroke="#29322b" stroke-width="5"/></g>
  </svg>`;
}

function triggerMascotDance() {
  if (document.querySelector("#mascot-dance")) return;
  const scene = document.createElement("div");
  scene.id = "mascot-dance";
  scene.className = "mascot-dance";
  scene.setAttribute("role", "status");
  scene.setAttribute("aria-label", uiLabel("mascotDance"));
  scene.innerHTML = `<div class="mascot-dance-stage"><span class="dance-beam beam-left"></span><span class="dance-beam beam-right"></span><span class="dance-particles" aria-hidden="true"><i></i><b></b><em></em><strong></strong></span><span class="dance-figure dance-partner-figure">${partnerDanceSprite()}</span><span class="dance-figure dance-scout-figure">${scoutDanceSprite()}</span></div>`;
  document.body.append(scene);
  window.setTimeout(() => scene.remove(), 10400);
}

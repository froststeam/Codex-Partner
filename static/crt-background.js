(() => {
  "use strict";
  const canvas = document.getElementById("crt-background");
  if (!canvas) return;
  const gl = canvas.getContext("webgl", { alpha: true, antialias: false, powerPreference: "low-power" });
  if (!gl) return;

  const vertexSource = `attribute vec2 p; void main() { gl_Position = vec4(p, 0.0, 1.0); }`;
  const fragmentSource = `
    precision mediump float;
    uniform vec2 iResolution;
    uniform float iTime;
    float hash(vec2 p) { p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32); return fract(p.x * p.y); }
    float noise(vec2 p) {
      vec2 i = floor(p), f = smoothstep(0.0, 1.0, fract(p));
      float a = hash(i), b = hash(i + vec2(1.0, 0.0)), c = hash(i + vec2(0.0, 1.0)), d = hash(i + vec2(1.0, 1.0));
      return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
    }
    void main() {
      vec2 uv = gl_FragCoord.xy / iResolution.xy;
      vec2 p = (gl_FragCoord.xy - 0.5 * iResolution.xy) / iResolution.y;
      float glow = exp(-dot(p * vec2(0.7, 0.9), p * vec2(0.7, 0.9)) * 1.8);
      float glass = 0.5 + 0.5 * sin(p.y * 2.2 + iTime * 0.04);
      vec3 col = vec3(0.006, 0.035, 0.014) + vec3(0.006, 0.038, 0.014) * glow;
      col += vec3(0.001, 0.006, 0.002) * glass;
      // Same vignette shape as the supplied Shadertoy shader.
      float frame = 16.0 * uv.x * uv.y * (1.0 - uv.x) * (1.0 - uv.y);
      float vignette = 0.5 + 0.5 * pow(max(frame, 0.001), 0.2);
      float scanline = 0.985 + 0.015 * sin(gl_FragCoord.y * 1.55);
      gl_FragColor = vec4(max(col * vignette * scanline, 0.0), 0.72);
    }
  `;
  function compile(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) { gl.deleteShader(shader); return null; }
    return shader;
  }
  const vertex = compile(gl.VERTEX_SHADER, vertexSource), fragment = compile(gl.FRAGMENT_SHADER, fragmentSource);
  if (!vertex || !fragment) return;
  const program = gl.createProgram();
  gl.attachShader(program, vertex); gl.attachShader(program, fragment); gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return;
  gl.useProgram(program);
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const position = gl.getAttribLocation(program, "p");
  gl.enableVertexAttribArray(position); gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
  const resolution = gl.getUniformLocation(program, "iResolution"), time = gl.getUniformLocation(program, "iTime");
  let active = false, frameId = 0, lastFrame = 0;
  function resize() {
    const scale = Math.min(window.devicePixelRatio || 1, 1.25);
    const width = Math.max(1, Math.floor(innerWidth * scale)), height = Math.max(1, Math.floor(innerHeight * scale));
    if (canvas.width === width && canvas.height === height) return;
    canvas.width = width; canvas.height = height; gl.viewport(0, 0, width, height);
  }
  function draw(now) {
    frameId = 0;
    if (!active || document.hidden) return;
    if (now - lastFrame < 33) { frameId = requestAnimationFrame(draw); return; }
    lastFrame = now; resize(); gl.uniform2f(resolution, canvas.width, canvas.height); gl.uniform1f(time, now * 0.001); gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4); frameId = requestAnimationFrame(draw);
  }
  function syncTheme() {
    active = document.documentElement.dataset.theme === "wasteland";
    canvas.classList.toggle("active", active);
    if (active && !frameId) frameId = requestAnimationFrame(draw);
  }
  new MutationObserver(syncTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  addEventListener("resize", resize, { passive: true });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && active && !frameId) frameId = requestAnimationFrame(draw); });
  resize(); syncTheme();
})();

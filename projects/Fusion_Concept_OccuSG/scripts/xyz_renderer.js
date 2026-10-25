/* Dependency-free WebGL point-cloud viewer for causal timeline revisions. */
(function () {
  "use strict";

  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
  const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const scale = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const norm = a => Math.sqrt(dot(a, a));
  const normalize = a => { const n = norm(a) || 1; return scale(a, 1 / n); };

  // Revision publication is a two-phase operation: fetch/validate everything,
  // then synchronously commit one bundle. A newer request invalidates older
  // promises, including promises that finish out of order.
  function createAtomicRevisionGate() {
    let generation = 0;
    return {
      async request(load, commit, onError) {
        const token = ++generation;
        try {
          const bundle = await load();
          if (token !== generation) return { status: "stale" };
          commit(bundle);
          if (token !== generation) return { status: "stale" };
          return { status: "committed" };
        } catch (error) {
          if (token !== generation) return { status: "stale" };
          if (typeof onError === "function") onError(error);
          return { status: "failed", error };
        }
      },
      cancel() { generation += 1; },
    };
  }

  function perspective(fovy, aspect, near, far) {
    const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
    return new Float32Array([
      f / aspect, 0, 0, 0,
      0, f, 0, 0,
      0, 0, (far + near) * nf, -1,
      0, 0, 2 * far * near * nf, 0,
    ]);
  }
  function lookAt(eye, target, up) {
    const z = normalize(sub(eye, target)), x = normalize(cross(up, z)), y = cross(z, x);
    return new Float32Array([
      x[0], y[0], z[0], 0,
      x[1], y[1], z[1], 0,
      x[2], y[2], z[2], 0,
      -dot(x, eye), -dot(y, eye), -dot(z, eye), 1,
    ]);
  }
  function multiply(a, b) {
    const out = new Float32Array(16);
    for (let col = 0; col < 4; col++) for (let row = 0; row < 4; row++) {
      out[col * 4 + row] = a[row] * b[col * 4] + a[4 + row] * b[col * 4 + 1] + a[8 + row] * b[col * 4 + 2] + a[12 + row] * b[col * 4 + 3];
    }
    return out;
  }
  function transformPoint(m, p) {
    const x = p[0], y = p[1], z = p[2];
    const cx = m[0] * x + m[4] * y + m[8] * z + m[12];
    const cy = m[1] * x + m[5] * y + m[9] * z + m[13];
    const cz = m[2] * x + m[6] * y + m[10] * z + m[14];
    const cw = m[3] * x + m[7] * y + m[11] * z + m[15];
    return cw > 0 ? [cx / cw, cy / cw, cz / cw] : null;
  }

  function compile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader) || "shader compile failed");
    return shader;
  }
  function makeProgram(gl) {
    const vs = compile(gl, gl.VERTEX_SHADER, `
      attribute vec3 aPosition; attribute vec3 aColor;
      uniform mat4 uViewProjection; uniform float uPointSize;
      varying vec3 vColor;
      void main(){ gl_Position=uViewProjection*vec4(aPosition,1.0); gl_PointSize=uPointSize; vColor=aColor; }
    `);
    const fs = compile(gl, gl.FRAGMENT_SHADER, `
      precision mediump float; varying vec3 vColor;
      void main(){ gl_FragColor=vec4(vColor,1.0); }
    `);
    const program = gl.createProgram();
    gl.attachShader(program, vs); gl.attachShader(program, fs); gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program) || "program link failed");
    return program;
  }

  function niceLength(value) {
    const raw = Math.max(0.2, value), p = Math.pow(10, Math.floor(Math.log10(raw))), q = raw / p;
    return (q < 1.5 ? 1 : q < 3.5 ? 2 : q < 7.5 ? 5 : 10) * p;
  }

  class XyzViewer {
    constructor(canvas, options) {
      this.canvas = canvas; this.options = options || {}; this.gl = canvas.getContext("webgl", { antialias: true, alpha: false, depth: true }) || canvas.getContext("experimental-webgl");
      this.revision = null; this.scene = null; this.disposed = false; this.cameraReady = false;
      this.camera = { target: [0, 0, 0], yaw: 0.75, pitch: 0.48, distance: 4, fitDistance: 4, radius: 2 };
      this.pointer = null; this.matrix = null; this.pending = false;
      this.panMode = false;
      this.diagnostics = { renderer: "WebGL", roomVolumes: false, frustum: false, cacheEntries: 1 };
      this.overlay = document.getElementById("xyzOverlay"); this.status = document.getElementById("xyzStatus");
      this.gizmoCanvas = document.getElementById("xyzGizmo"); this.gizmoContext = this.gizmoCanvas?.getContext("2d") || null;
      this.panButton = document.getElementById("panXYZ");
      if (!this.gl) { this.error = "WebGL is unavailable in this browser."; this.setStatus(this.error); return; }
      try { this.program = makeProgram(this.gl); } catch (e) { this.error = String(e); this.setStatus(`WebGL setup failed: ${this.error}`); return; }
      const gl = this.gl; this.locations = {
        position: gl.getAttribLocation(this.program, "aPosition"), color: gl.getAttribLocation(this.program, "aColor"),
        matrix: gl.getUniformLocation(this.program, "uViewProjection"), size: gl.getUniformLocation(this.program, "uPointSize"),
      };
      this.buffers = { points: gl.createBuffer(), colors: gl.createBuffer(), trajectory: gl.createBuffer(), trajectoryColors: gl.createBuffer(), lines: gl.createBuffer(), lineColors: gl.createBuffer() };
      gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL); gl.clearColor(0.965, 0.973, 0.984, 1);
      this.bindControls();
      if (this.panButton) this.panButton.addEventListener("click", () => {
        this.panMode = !this.panMode; this.panButton.textContent = `Pan mode: ${this.panMode ? "ON" : "OFF"}`;
        this.panButton.setAttribute("aria-pressed", String(this.panMode)); this.panButton.classList.toggle("active", this.panMode);
      });
      this.resizeObserver = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => this.draw()) : null;
      if (this.resizeObserver) this.resizeObserver.observe(canvas);
      window.addEventListener("resize", this.onResize = () => this.draw());
      this.setStatus("XYZ world view ready · drag orbit · Shift/right-drag pan · wheel zoom.");
    }
    setStatus(text) { if (this.status) this.status.textContent = text; }
    validate(data) {
      if (this.disposed) throw new Error("XYZ renderer is disposed");
      if (this.error || !this.gl) throw new Error(this.error || "XYZ renderer is unavailable");
      if (!data || !data.geometry || !Array.isArray(data.geometry.objects)) throw new Error("XYZ geometry payload is missing objects");
      for (const object of data.geometry.objects) {
        const points = object.points || [], colors = object.colors || [];
        if (!Array.isArray(points) || points.length !== Number(object.sampled_points ?? points.length)) throw new Error(`invalid XYZ display count for ${object.hypothesis_id}`);
        if (colors.length && colors.length !== points.length) throw new Error(`XYZ point/color count mismatch for ${object.hypothesis_id}`);
        for (const point of points) if (!Array.isArray(point) || point.length !== 3 || point.some(value => !Number.isFinite(Number(value)))) throw new Error(`invalid XYZ point for ${object.hypothesis_id}`);
      }
      const posePoints = (data.trajectory || []).filter(row => row.pose);
      for (const row of posePoints) {
        if (!Array.isArray(row.pose) || row.pose.length !== 4 || row.pose.some(line => !Array.isArray(line) || line.length !== 4 || line.some(value => !Number.isFinite(Number(value))))) throw new Error(`invalid trajectory pose at ${row.frameId}`);
      }
      if (data.cameraPose && (!Array.isArray(data.cameraPose) || data.cameraPose.length !== 4 || data.cameraPose.some(line => !Array.isArray(line) || line.length !== 4 || line.some(value => !Number.isFinite(Number(value)))))) throw new Error("invalid current camera pose");
      return true;
    }
    bindControls() {
      const canvas = this.canvas;
      canvas.addEventListener("contextmenu", e => e.preventDefault());
      canvas.addEventListener("pointerdown", e => {
        this.pointer = { x: e.clientX, y: e.clientY, startX: e.clientX, startY: e.clientY, button: e.button, pan: this.panMode || e.button === 2 || e.button === 1 || e.shiftKey };
        canvas.setPointerCapture?.(e.pointerId);
      });
      canvas.addEventListener("pointermove", e => {
        if (!this.pointer) return;
        const dx = e.clientX - this.pointer.x, dy = e.clientY - this.pointer.y;
        this.pointer.x = e.clientX; this.pointer.y = e.clientY;
        if (this.pointer.pan) this.pan(dx, dy); else { this.camera.yaw -= dx * 0.008; this.camera.pitch = clamp(this.camera.pitch + dy * 0.008, -1.48, 1.48); }
        this.draw();
      });
      canvas.addEventListener("pointerup", e => {
        if (!this.pointer) return;
        const p = this.pointer, moved = Math.hypot(e.clientX - p.startX, e.clientY - p.startY);
        this.pointer = null;
        if (moved < 4 && p.button === 0 && !p.pan) this.pick(e.clientX, e.clientY);
      });
      canvas.addEventListener("pointercancel", () => { this.pointer = null; });
      canvas.addEventListener("wheel", e => { e.preventDefault(); this.camera.distance = clamp(this.camera.distance * Math.exp(e.deltaY * 0.001), this.camera.radius * 0.15, this.camera.radius * 250 + 2); this.draw(); }, { passive: false });
    }
    eye() {
      const c = this.camera, cp = Math.cos(c.pitch), sp = Math.sin(c.pitch), sy = Math.sin(c.yaw), cy = Math.cos(c.yaw);
      return add(c.target, [c.distance * cp * sy, -c.distance * cp * cy, c.distance * sp]);
    }
    pan(dx, dy) {
      const eye = this.eye(), forward = normalize(sub(this.camera.target, eye)), right = normalize(cross(forward, [0, 0, 1])), up = normalize(cross(right, forward));
      const step = this.camera.distance / Math.max(1, this.canvas.clientHeight) * 1.7;
      this.camera.target = add(this.camera.target, add(scale(right, -dx * step), scale(up, dy * step)));
    }
    fit() {
      if (!this.scene) return;
      const all = this.scene.points.concat(this.scene.trajectory);
      if (!all.length) { this.camera.target = [0, 0, 0]; this.camera.radius = 2; this.camera.distance = this.camera.fitDistance = 4; }
      else {
        const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
        for (const p of all) for (let i = 0; i < 3; i++) { lo[i] = Math.min(lo[i], p[i]); hi[i] = Math.max(hi[i], p[i]); }
        this.camera.target = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2];
        this.camera.radius = Math.max(0.5, norm(sub(hi, lo)) / 2);
        this.camera.fitDistance = this.camera.distance = Math.max(1.25, this.camera.radius * 2.8);
      }
      this.cameraReady = true; this.draw();
    }
    reset() {
      this.camera.yaw = 0.75; this.camera.pitch = 0.48;
      this.camera.target = this.scene?.center?.slice() || [0, 0, 0];
      this.camera.radius = this.scene?.radius || 2;
      this.camera.distance = this.camera.fitDistance = Math.max(1.25, this.camera.radius * 2.8);
      this.draw();
    }
    update(data) {
      if (this.disposed || this.error) return;
      this.validate(data);
      const objects = data.geometry?.objects || [], snapshotById = new Map((data.snapshot?.objects || []).map(o => [o.hypothesis_id, o]));
      const points = [], colors = [], objectIds = [], objectRows = new Map(), sampled = [];
      for (const g of objects) {
        const meta = snapshotById.get(g.hypothesis_id);
        if (!meta) continue;
        const xyz = g.points || [], rgb = g.colors || [];
        if (xyz.length !== Number(g.sampled_points ?? xyz.length)) throw new Error(`Invalid display count for ${g.hypothesis_id}`);
        const start = points.length;
        for (let i = 0; i < xyz.length; i++) {
          const p = xyz[i]; if (!Array.isArray(p) || p.length !== 3 || p.some(v => !Number.isFinite(v))) continue;
          points.push(p); objectIds.push(g.hypothesis_id);
          const c = rgb[i] || [150, 170, 190]; colors.push(c.map(v => clamp(Number(v) / 255, 0, 1)));
        }
        objectRows.set(g.hypothesis_id, { object: meta, geometry: g, start, end: points.length });
        sampled.push({ id: g.hypothesis_id, shown: points.length - start, total: Number(g.source_points ?? xyz.length), sha256: g.source_sha256 || null });
      }
      const path = (data.trajectory || []).filter(row => row.pose).map(row => [Number(row.pose[0][3]), Number(row.pose[1][3]), Number(row.pose[2][3])]).filter(p => p.every(Number.isFinite));
      const bounds = this.bounds(points.concat(path));
      const selectedState = (data.graph?.objects || []).find(o => (o.snapshot_hypothesis_id || o.hypothesis_id) === data.selectedObject);
      this.scene = { points, colors, objectIds, objectRows, trajectory: path, trajectoryFrames: (data.trajectory || []).filter(row => row.pose).map(row => row.frameId), camera: data.cameraPose ? [data.cameraPose[0][3], data.cameraPose[1][3], data.cameraPose[2][3]] : null, sampled, bounds, center: bounds.center, radius: bounds.radius, revision: data.revision, frameId: data.frameId, stage: data.stage, finalMode: !!data.finalMode, selectedObject: data.selectedObject, membershipStatus: data.membershipStatus || "unavailable", membershipReason: data.membershipReason || "reason unavailable", roomSemanticsStatus: data.roomSemanticsStatus || "unavailable", prefixAdmissionStatus: selectedState?.prefix_admission?.status || "unavailable" };
      if (!this.cameraReady) this.fit(); else this.draw();
      const shown = sampled.reduce((n, x) => n + x.shown, 0), total = sampled.reduce((n, x) => n + x.total, 0);
      const rows = sampled.map(x => `${x.id}: ${x.shown}/${x.total}`).join(" · ") || "no object point samples";
      const stage = data.finalMode ? "final postprocess (terminal)" : `${data.stage} · prefix state`;
      const ruler = niceLength(bounds.radius * 0.65), axis = Math.max(0.25, Math.min(1, bounds.radius * 0.35));
      if (this.overlay) this.overlay.textContent = `${data.frameId} · rev ${data.revision} · ${stage} · trajectory through this frame · no camera frustum\nAxes X/Y/Z in m · ruler ${ruler.toFixed(2)} m (X red, Y green, Z blue)`;
      this.setStatus(`Frame ${data.frameId} · object/geometry rev ${data.revision} · ${points.length} display points / ${total} source points across ${sampled.length} objects · ${path.length} camera poses · prefix admission ${this.scene.prefixAdmissionStatus} · membership ${this.scene.membershipStatus} (${this.scene.membershipReason}) · room semantics ${this.scene.roomSemanticsStatus} · ${rows}`);
      this.diagnostics = { renderer: "WebGL", revision: data.revision, frameId: data.frameId, stage: data.stage, finalMode: !!data.finalMode, objectIds: [...objectRows.keys()], selectedObject: data.selectedObject || null, trajectoryFrameIds: this.scene.trajectoryFrames.slice(), trajectoryPointCount: path.length, displayedPointCount: points.length, sourcePointCount: total, objectPointCounts: sampled.map(x => ({ id: x.id, shown: x.shown, total: x.total })), axesUnits: "m", axisLengthM: axis, rulerLengthM: ruler, worldFrame: "conceptgraphs_world_from_capture_contract", roomVolumes: false, frustum: false, cacheEntries: 1 };
      window.__xyzDiagnostics = this.diagnostics;
    }
    bounds(points) {
      if (!points.length) return { center: [0, 0, 0], radius: 2, lo: [-1, -1, -1], hi: [1, 1, 1] };
      const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
      for (const p of points) for (let i = 0; i < 3; i++) { lo[i] = Math.min(lo[i], p[i]); hi[i] = Math.max(hi[i], p[i]); }
      const center = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2];
      return { center, radius: Math.max(0.5, norm(sub(hi, lo)) / 2), lo, hi };
    }
    draw() {
      if (!this.gl || this.error || !this.scene) return;
      const gl = this.gl, canvas = this.canvas, dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      const width = Math.max(1, Math.round(canvas.clientWidth * dpr)), height = Math.max(1, Math.round(canvas.clientHeight * dpr));
      if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
      gl.viewport(0, 0, width, height); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT); gl.useProgram(this.program);
      const radius = this.camera.radius || 2, eye = this.eye(), view = lookAt(eye, this.camera.target, [0, 0, 1]);
      const proj = perspective(Math.PI / 4, width / height, Math.max(0.01, this.camera.distance / 1000), Math.max(100, this.camera.distance + radius * 1000));
      const matrix = multiply(proj, view); this.matrix = matrix; this.drawGizmo(view); gl.uniformMatrix4fv(this.locations.matrix, false, matrix); gl.enableVertexAttribArray(this.locations.position); gl.enableVertexAttribArray(this.locations.color);
      this.drawLines(this.scene);
      this.drawArray(this.buffers.trajectory, this.buffers.trajectoryColors, this.scene.trajectory, this.scene.trajectory.map(() => [0.95, 0.2, 0.36]), gl.LINE_STRIP, 2.5);
      this.drawArray(this.buffers.points, this.buffers.colors, this.scene.points, this.scene.colors, gl.POINTS, 4.0);
      if (this.scene.camera) this.drawArray(this.buffers.trajectory, this.buffers.trajectoryColors, [this.scene.camera], [[1.0, 0.72, 0.05]], gl.POINTS, 10.0);
      if (this.scene.selectedObject && this.scene.objectRows.has(this.scene.selectedObject)) {
        const row = this.scene.objectRows.get(this.scene.selectedObject), points = this.scene.points.slice(row.start, row.end);
        this.drawArray(this.buffers.trajectory, this.buffers.trajectoryColors, points, points.map(() => [1, 0.82, 0.12]), gl.POINTS, 6.5);
      }
    }
    drawArray(positionBuffer, colorBuffer, points, colors, mode, size) {
      const gl = this.gl; if (!points.length) return;
      const p = new Float32Array(points.flat()), c = new Float32Array(colors.flat());
      gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer); gl.bufferData(gl.ARRAY_BUFFER, p, gl.DYNAMIC_DRAW);
      gl.vertexAttribPointer(this.locations.position, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, colorBuffer); gl.bufferData(gl.ARRAY_BUFFER, c, gl.DYNAMIC_DRAW);
      gl.vertexAttribPointer(this.locations.color, 3, gl.FLOAT, false, 0, 0);
      gl.uniform1f(this.locations.size, size); gl.drawArrays(mode, 0, points.length);
    }
    drawLines(scene) {
      const o = this.camera.target, axis = Math.max(0.25, Math.min(1, scene.radius * 0.35)), linePoints = [], lineColors = [];
      const seg = (a, b, c) => { linePoints.push(a, b); lineColors.push(c, c); };
      seg(o, add(o, [axis, 0, 0]), [0.9, 0.16, 0.12]); seg(o, add(o, [0, axis, 0]), [0.08, 0.55, 0.24]); seg(o, add(o, [0, 0, axis]), [0.12, 0.34, 0.88]);
      const ruler = niceLength(scene.radius * 0.65), base = [scene.center[0] - ruler / 2, scene.center[1] - scene.radius * 0.32, scene.center[2] - scene.radius * 0.32], tick = ruler * 0.06;
      seg(base, add(base, [ruler, 0, 0]), [0.12, 0.16, 0.2]);
      seg(base, add(base, [0, 0, tick]), [0.12, 0.16, 0.2]); seg(add(base, [ruler, 0, 0]), add(add(base, [ruler, 0, 0]), [0, 0, tick]), [0.12, 0.16, 0.2]);
      this.drawArray(this.buffers.lines, this.buffers.lineColors, linePoints, lineColors, this.gl.LINES, 1);
      if (this.overlay) this.overlay.title = `World axes XYZ; coordinates in metres. RGB axis segments ${axis.toFixed(2)} m; ruler ${ruler.toFixed(2)} m.`;
      this.diagnostics = { ...this.diagnostics, axisLengthM: axis, rulerLengthM: ruler, boundsXYZ: [scene.bounds.lo, scene.bounds.hi] };
      window.__xyzDiagnostics = this.diagnostics;
    }
    drawGizmo(view) {
      if (!this.gizmoCanvas || !this.gizmoContext) return;
      const canvas = this.gizmoCanvas, ctx = this.gizmoContext, dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      const w = canvas.clientWidth || 104, h = canvas.clientHeight || 104;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
      const origin = [w * 0.48, h * 0.57], axes = [
        { name: "X", v: [1, 0, 0], color: "#dc2626" },
        { name: "Y", v: [0, 1, 0], color: "#16a34a" },
        { name: "Z", v: [0, 0, 1], color: "#2563eb" },
      ];
      ctx.font = "bold 13px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      for (const axis of axes) {
        const v = axis.v, vx = view[0] * v[0] + view[4] * v[1] + view[8] * v[2], vy = view[1] * v[0] + view[5] * v[1] + view[9] * v[2];
        let dx = vx, dy = -vy, n = Math.hypot(dx, dy);
        if (n < 1e-4) { dx = axis.name === "Y" ? 1 : 0; dy = axis.name === "Z" ? -1 : axis.name === "X" ? -1 : 0; n = Math.hypot(dx, dy); }
        const end = [origin[0] + dx / n * 31, origin[1] + dy / n * 31];
        ctx.beginPath(); ctx.moveTo(origin[0], origin[1]); ctx.lineTo(end[0], end[1]); ctx.strokeStyle = axis.color; ctx.lineWidth = 3; ctx.stroke();
        ctx.beginPath(); ctx.arc(end[0], end[1], 3.5, 0, Math.PI * 2); ctx.fillStyle = axis.color; ctx.fill();
        ctx.fillStyle = axis.color; ctx.fillText(axis.name, end[0] + dx / n * 9, end[1] + dy / n * 9);
      }
      ctx.beginPath(); ctx.arc(origin[0], origin[1], 3, 0, Math.PI * 2); ctx.fillStyle = "#172033"; ctx.fill();
      ctx.font = "10px system-ui"; ctx.fillStyle = "#334155"; ctx.textAlign = "center"; ctx.fillText("world", w * 0.5, h - 8);
      this.diagnostics = { ...this.diagnostics, axisGizmo: "world orientation projected from current camera view" };
      window.__xyzDiagnostics = this.diagnostics;
    }
    pick(clientX, clientY) {
      if (!this.scene || !this.matrix) return;
      const rect = this.canvas.getBoundingClientRect(), px = clientX - rect.left, py = clientY - rect.top; let best = null;
      for (let i = 0; i < this.scene.points.length; i++) {
        const ndc = transformPoint(this.matrix, this.scene.points[i]); if (!ndc || ndc.some(v => v < -1.1 || v > 1.1)) continue;
        const sx = (ndc[0] + 1) * 0.5 * rect.width, sy = (1 - ndc[1]) * 0.5 * rect.height, d = Math.hypot(sx - px, sy - py);
        if (d <= 13 && (!best || d < best.distance || (d === best.distance && ndc[2] < best.depth))) best = { distance: d, depth: ndc[2], objectId: this.scene.objectIds[i] };
      }
      if (best && typeof this.options.onPick === "function") this.options.onPick(best.objectId, this.scene.revision);
    }
    dispose() {
      if (this.disposed) return; this.disposed = true;
      if (this.resizeObserver) this.resizeObserver.disconnect(); if (this.onResize) window.removeEventListener("resize", this.onResize);
      if (this.gl) { for (const buffer of Object.values(this.buffers || {})) this.gl.deleteBuffer(buffer); if (this.program) this.gl.deleteProgram(this.program); this.gl.getExtension("WEBGL_lose_context")?.loseContext(); }
    }
  }

  window.createXyzViewer = (canvas, options) => new XyzViewer(canvas, options);
  window.createAtomicRevisionGate = createAtomicRevisionGate;
  window.XYZ_VIEWER_MATH = { perspective, lookAt, multiply, transformPoint, niceLength };
})();

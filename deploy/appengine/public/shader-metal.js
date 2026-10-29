/* ClarityOS — liquid metal ribbons (WebGL).
 *
 * Vanilla port of a React/Tailwind component: this project has no bundler,
 * JSX or Tailwind, so the React wrapper and the `cn` helper are dropped and
 * the engine is exposed as a global factory instead. The GLSL and the render
 * loop are unchanged.
 *
 *   const metal = createShaderMetal(canvas, { metal: "chrome", ribbons: 3 });
 *
 * Returns null when WebGL is unavailable, so callers can keep their old
 * backdrop as a fallback. Pauses off-screen and in hidden tabs, holds a still
 * frame under prefers-reduced-motion, and recovers from a lost GL context.
 */
(function (global) {
  "use strict";

  var METALS = {
    chrome: "#F2F3F5",
    gold: "#FFD38A",
    copper: "#F7AE92",
    graphite: "#8E9096",
  };

  var DEFAULT_METAL = "chrome";
  var DEFAULT_RIBBONS = 3;
  var DEFAULT_SPEED = 0.3;
  var DEFAULT_IRIDESCENCE = 0.2;

  /** Most pixels we'll shade per frame; past this the canvas renders softer. */
  var MAX_PIXELS = 2400000;
  /** Where the key light rests when the pointer is away: up and to the left. */
  var REST_LIGHT = { x: -0.45, y: 0.55 };

  var VERT = [
    "attribute vec2 a_position;",
    "void main() {",
    "  gl_Position = vec4(a_position, 0.0, 1.0);",
    "}",
  ].join("\n");

  var FRAG = `
precision highp float;

uniform vec2 u_resolution;
uniform float u_time;
uniform float u_count;
uniform float u_iridescence;
uniform float u_pull;
uniform float u_dark;
uniform vec2 u_mouse;
uniform vec2 u_light;
uniform vec3 u_f0;

const float TAU = 6.28318530718;
const float TILT = -0.32;

float hash(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

// Smooth 1D value noise, for the brushed grain that runs along each ribbon.
float lines(float x, float seed) {
  float i = floor(x);
  float f = fract(x);
  f = f * f * (3.0 - 2.0 * f);
  return mix(hash(vec2(i, seed)), hash(vec2(i + 1.0, seed)), f);
}

vec2 rotate(vec2 p, float a) {
  float c = cos(a);
  float s = sin(a);
  return vec2(c * p.x - s * p.y, s * p.x + c * p.y);
}

// The spine of ribbon k: three swells at different speeds, so it never
// settles into a loop. Near the pointer it leans toward it.
float spine(float x, float k, float lane, float t, vec2 m) {
  float y = lane;
  y += 0.13 * sin(1.15 * x + 0.55 * t + 2.1 * k);
  y += 0.06 * sin(2.6 * x - 0.8 * t + 1.3 * k + 0.7);
  y += 0.025 * sin(5.3 * x + 1.3 * t + 4.0 * k);
  float near = exp(-(x - m.x) * (x - m.x) * 5.0);
  y += (m.y - y) * 0.22 * u_pull * near;
  return y;
}

// A studio to reflect: sky and floor meet at a crisp horizon, a big
// softbox overhead, two strip lights and the key light. Linear light.
vec3 studio(vec3 r, float t) {
  float a = 0.28 * sin(t * 0.11);
  r.xz = rotate(r.xz, a);
  float y = r.y;

  vec3 top = mix(vec3(0.82, 0.83, 0.86), vec3(0.025, 0.025, 0.028), u_dark);
  vec3 horizon = mix(vec3(1.0), vec3(0.16, 0.16, 0.17), u_dark);
  vec3 ground = mix(vec3(0.05, 0.05, 0.055), vec3(0.004), u_dark);
  vec3 sky = mix(horizon, top, smoothstep(0.0, 0.85, y));
  vec3 floor_ = mix(ground * 2.4, ground, smoothstep(0.0, -0.5, y));
  vec3 c = mix(floor_, sky, smoothstep(-0.025, 0.025, y));

  // Overhead softbox, feathered.
  float box = smoothstep(0.6, 0.45, abs(r.x)) * smoothstep(0.42, 0.55, y) *
              smoothstep(1.0, 0.85, y);
  c += box * mix(1.6, 2.2, u_dark);

  // Tall strip lights left and right: warm and cool, faintly.
  float stripL = smoothstep(0.1, 0.03, abs(r.x + 0.78)) * smoothstep(-0.35, -0.1, y) * smoothstep(0.8, 0.5, y);
  float stripR = smoothstep(0.08, 0.02, abs(r.x - 0.72)) * smoothstep(-0.3, -0.05, y) * smoothstep(0.75, 0.45, y);
  c += stripL * vec3(1.25, 1.1, 0.95) * mix(1.0, 1.5, u_dark);
  c += stripR * vec3(0.9, 1.05, 1.3) * mix(0.8, 1.3, u_dark);

  // A low bounce card, so the undersides aren't dead.
  float card = smoothstep(0.7, 0.2, abs(r.x)) * smoothstep(-0.95, -0.7, y) * smoothstep(-0.4, -0.65, y);
  c += card * mix(0.35, 0.18, u_dark);
  return c;
}

vec3 tonemap(vec3 x) {
  x = max(x, 0.0);
  return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
}

vec3 linearToSrgb(vec3 c) {
  c = clamp(c, 0.0, 1.0);
  vec3 lo = c * 12.92;
  vec3 hi = 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055;
  return mix(lo, hi, step(vec3(0.0031308), c));
}

void main() {
  vec2 frag = gl_FragCoord.xy;
  float px = 1.0 / u_resolution.y;
  vec2 p = rotate((frag - 0.5 * u_resolution) * px, TILT);
  vec2 m = rotate((u_mouse - 0.5) * u_resolution * px, TILT);
  float t = u_time;

  vec3 L = normalize(vec3(u_light, 0.85));
  float count = u_count;
  float spread = mix(0.0, 0.3, clamp((count - 1.0) / 4.0, 0.0, 1.0)) + 0.08 * step(1.5, count);
  float baseWidth = mix(0.13, 0.075, clamp((count - 1.0) / 4.0, 0.0, 1.0));

  // Premultiplied, composited back to front.
  vec3 col = vec3(0.0);
  float alpha = 0.0;

  for (int i = 0; i < 5; i++) {
    float k = float(i);
    if (k >= count) break;
    float f = count > 1.0 ? k / (count - 1.0) : 0.5;
    float lane = (f - 0.5) * spread;
    // Farther ribbons drift a touch slower: a hint of depth.
    float tk = t * mix(0.82, 1.0, f);

    float x = p.x;
    float e = 0.004;
    float c = spine(x, k, lane, tk, m);
    float slope = (spine(x + e, k, lane, tk, m) - spine(x - e, k, lane, tk, m)) / (2.0 * e);
    float stretch = inversesqrt(1.0 + slope * slope);
    float d = (p.y - c) * stretch;

    // Twist along the length; width breathes.
    float theta = 1.5 * x + 0.45 * tk + 1.7 * k + 0.55 * sin(0.7 * x - 0.3 * tk + k);
    float ct = cos(theta);
    float st = sin(theta);
    float hw = baseWidth * (0.82 + 0.22 * sin(0.9 * x + 0.35 * tk + 2.3 * k));
    // Seen edge-on a ribbon still has a little thickness.
    float proj = hw * max(abs(ct), 0.035);

    // Soft contact shadow on whatever is behind, before the ribbon lands.
    float sd = d + 0.045;
    float shadowW = proj + 0.05;
    float shadow = (1.0 - smoothstep(0.35, 1.0, abs(sd) / shadowW)) * mix(0.2, 0.32, u_dark);
    col *= 1.0 - shadow;
    alpha = alpha + shadow * (1.0 - alpha);

    float v = d / (ct >= 0.0 ? proj : -proj);
    float aa = 1.2 * px / proj;
    float cover = 1.0 - smoothstep(1.0 - aa, 1.0 + aa, abs(v));
    if (cover <= 0.0) continue;

    // Ribbon frame: T along, N2 across in the screen plane, Z toward us.
    vec3 T = normalize(vec3(1.0, slope, 0.0));
    vec3 N2 = normalize(vec3(-slope, 1.0, 0.0));
    vec3 B = ct * N2 + st * vec3(0.0, 0.0, 1.0);
    vec3 n = -st * N2 + ct * vec3(0.0, 0.0, 1.0);

    // A shallow arc across the width, rolling over at the edges, plus
    // slow ripples running down the length and a brushed grain.
    float roll = v * 0.45 + v * v * v * 0.5;
    float ripple = sin(x * 13.0 - tk * 2.2 + k * 3.1) * 0.06 + sin(x * 31.0 + tk * 1.3 + k) * 0.015;
    float grain = lines(v * 34.0 + k * 17.0, k) - 0.5;
    n = normalize(n + B * (roll + grain * 0.015) + T * ripple);
    // Edge-on, what shows is the strip's polished edge. It faces us, tipped
    // up a little so it catches the sky rather than sitting on the horizon.
    float edge = smoothstep(0.14, 0.03, abs(ct));
    vec3 rim = B * (st >= 0.0 ? 1.0 : -1.0) + vec3(0.0, 0.2, 0.0);
    n = normalize(mix(n, normalize(rim), edge));
    float back = step(n.z, 0.0);
    n *= 1.0 - 2.0 * back;

    vec3 r = reflect(vec3(0.0, 0.0, -1.0), n);
    vec3 env = studio(r, t);

    float facing = clamp(n.z, 0.0, 1.0);
    vec3 F = u_f0 + (1.0 - u_f0) * pow(1.0 - facing, 5.0);

    // Thin film: thickness drifts along the ribbon, and the colour shifts
    // with the viewing angle, strongest on the turns.
    float film = 0.5 + 0.5 * sin(x * 1.6 + v * 0.9 + tk * 0.35 + k * 1.9);
    vec3 rainbow = 0.5 + 0.5 * cos(TAU * (film * 1.6 + facing * 1.1 + vec3(0.0, 0.33, 0.67)));
    // Keep the film's brightness level, so it tints the metal, never dims it.
    rainbow = 0.2 + 0.8 * rainbow;
    rainbow /= max(dot(rainbow, vec3(0.2126, 0.7152, 0.0722)), 0.2);
    float sheen = u_iridescence * (0.45 + 0.55 * (1.0 - facing));
    F *= mix(vec3(1.0), rainbow, sheen);

    float rl = max(dot(r, L), 0.0);
    float spec = pow(rl, 90.0) * 5.0 + pow(rl, 14.0) * 0.55;

    vec3 lit = env * F + spec * F;
    lit *= mix(1.0, 0.72, back) * (0.94 + grain * 0.12);
    lit *= mix(0.86, 1.0, f);
    vec3 rgb = linearToSrgb(tonemap(lit * 1.05));

    col = col * (1.0 - cover) + rgb * cover;
    alpha = alpha * (1.0 - cover) + cover;
  }

  // A hair of dither, so the long gradients never band. Faint shadows
  // need it on alpha too, or they step into contour lines.
  float dither = (hash(frag + fract(t) * 91.0) - 0.5) / 255.0;
  col += dither * alpha;
  alpha = clamp(alpha + dither * step(0.0005, alpha) * 1.5, 0.0, 1.0);
  gl_FragColor = vec4(clamp(col, 0.0, 1.0), alpha);
}
`;

  function isDev() {
    return global.location && /^(127\.0\.0\.1|localhost)$/.test(global.location.hostname);
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function numberOr(value, fallback, min, max) {
    if (typeof value !== "number" || !isFinite(value)) return fallback;
    return clamp(value, min, max);
  }

  /** Hex -> linear RGB, for the metal's base reflectance. */
  function hexToLinear(hex) {
    var h = String(hex).replace("#", "").trim();
    var full = h.length === 3 ? h.split("").map(function (c) { return c + c; }).join("")
                              : (h + "000000").slice(0, 6);
    var n = parseInt(full, 16);
    var srgb = isNaN(n)
      ? [0.8, 0.8, 0.8]
      : [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
    return srgb.map(function (c) {
      return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
  }

  /** Resolves dark mode the way both ClarityOS pages express it. */
  function isDarkTheme() {
    if (typeof document === "undefined") return false;
    var root = document.documentElement;
    if (root.classList.contains("dark")) return true;
    if (root.classList.contains("light")) return false;
    var dataTheme = root.getAttribute("data-theme");
    if (dataTheme === "dark") return true;
    if (dataTheme === "light") return false;
    return global.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function resolveDark(theme) {
    if (theme === "dark") return true;
    if (theme === "light") return false;
    return isDarkTheme();
  }

  function compile(gl, type, source) {
    var shader = gl.createShader(type);
    if (!shader) return null;
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      if (isDev()) console.warn("ShaderMetal: shader failed to compile\n", gl.getShaderInfoLog(shader));
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  }

  function createShaderMetal(canvas, initial) {
    var options = Object.assign({ interactive: true, theme: "auto" }, initial || {});

    var mouse = { x: 0.5, y: 0.5 };
    var target = { x: 0.5, y: 0.5 };
    var light = { x: REST_LIGHT.x, y: REST_LIGHT.y };
    var lightTarget = { x: REST_LIGHT.x, y: REST_LIGHT.y };
    var pull = 0;
    var pullTarget = 0;
    var dark = resolveDark(options.theme || "auto");
    var darkMix = dark ? 1 : 0;
    if (options.onThemeChange) options.onThemeChange(dark);

    var gl = null;
    try {
      gl = canvas.getContext("webgl", {
        alpha: true,
        premultipliedAlpha: true,
        antialias: false,
        depth: false,
        stencil: false,
        powerPreference: "high-performance",
      });
    } catch (_) {
      gl = null;
    }
    if (!gl) return null;

    var vs = null, fs = null, program = null, buf = null;
    var uResolution, uTime, uCount, uIridescence, uPull, uDark, uMouse, uLight, uF0;

    // Builds every GL resource. Runs at startup and again after a lost
    // context is restored, since a restore hands back a blank context.
    function initGl() {
      vs = compile(gl, gl.VERTEX_SHADER, VERT);
      fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
      if (!vs || !fs) {
        if (vs) gl.deleteShader(vs);
        if (fs) gl.deleteShader(fs);
        return false;
      }

      program = gl.createProgram();
      if (!program) {
        gl.deleteShader(vs);
        gl.deleteShader(fs);
        return false;
      }
      gl.attachShader(program, vs);
      gl.attachShader(program, fs);
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        if (isDev()) console.warn("ShaderMetal: program failed to link\n", gl.getProgramInfoLog(program));
        gl.deleteProgram(program);
        gl.deleteShader(vs);
        gl.deleteShader(fs);
        return false;
      }
      gl.useProgram(program);

      buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(
        gl.ARRAY_BUFFER,
        new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
        gl.STATIC_DRAW
      );
      var loc = gl.getAttribLocation(program, "a_position");
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

      uResolution = gl.getUniformLocation(program, "u_resolution");
      uTime = gl.getUniformLocation(program, "u_time");
      uCount = gl.getUniformLocation(program, "u_count");
      uIridescence = gl.getUniformLocation(program, "u_iridescence");
      uPull = gl.getUniformLocation(program, "u_pull");
      uDark = gl.getUniformLocation(program, "u_dark");
      uMouse = gl.getUniformLocation(program, "u_mouse");
      uLight = gl.getUniformLocation(program, "u_light");
      uF0 = gl.getUniformLocation(program, "u_f0");
      return true;
    }
    if (!initGl()) return null;

    // Ribbon edges are hard, so this renders at device resolution (up to 2x).
    // Returns true when the backing store changed.
    function resize() {
      var parent = canvas.parentElement;
      if (!parent) return false;
      var w = parent.clientWidth;
      var h = parent.clientHeight;
      if (w <= 0 || h <= 0) return false;
      var ratio = Math.min(global.devicePixelRatio || 1, 2, Math.sqrt(MAX_PIXELS / (w * h)));
      var width = Math.max(1, Math.floor(w * ratio));
      var height = Math.max(1, Math.floor(h * ratio));
      canvas.style.width = w + "px";
      canvas.style.height = h + "px";
      wake();
      // Assigning a size clears the canvas, even the same size.
      if (width === canvas.width && height === canvas.height) return false;
      canvas.width = width;
      canvas.height = height;
      gl.viewport(0, 0, canvas.width, canvas.height);
      return true;
    }

    var f0Key = "";
    function syncMetal() {
      var hex = options.color || METALS[options.metal || DEFAULT_METAL] || METALS.chrome;
      if (hex === f0Key) return;
      f0Key = hex;
      gl.uniform3fv(uF0, hexToLinear(hex));
    }

    var mqReduce = global.matchMedia("(prefers-reduced-motion: reduce)");
    var onScreen = true;
    var raf = 0;
    var destroyed = false;
    var lost = false;
    var last = 0;
    var clock = 0;

    function visible() {
      return onScreen && document.visibilityState !== "hidden";
    }

    function wake() {
      // An observer callback can still arrive after destroy.
      if (raf || destroyed || lost) return;
      last = performance.now();
      raf = requestAnimationFrame(tick);
    }

    // Still easing toward the pointer or the theme, even with motion paused.
    function settling() {
      return (
        Math.abs(target.x - mouse.x) > 1e-3 ||
        Math.abs(target.y - mouse.y) > 1e-3 ||
        Math.abs(lightTarget.x - light.x) > 1e-3 ||
        Math.abs(lightTarget.y - light.y) > 1e-3 ||
        Math.abs(pullTarget - pull) > 1e-3 ||
        Math.abs((dark ? 1 : 0) - darkMix) > 1e-3
      );
    }

    function tick(now) {
      raf = 0;
      if (lost) return;
      var dt = Math.min(0.05, Math.max(0, (now - last) / 1000));
      last = now;
      if (!visible()) return;
      var flowing = !mqReduce.matches;
      if (flowing) clock += dt;

      // Frame-rate independent easing.
      var ease = function (rate) { return 1 - Math.exp(-dt * rate); };
      mouse.x += (target.x - mouse.x) * ease(3);
      mouse.y += (target.y - mouse.y) * ease(3);
      light.x += (lightTarget.x - light.x) * ease(5);
      light.y += (lightTarget.y - light.y) * ease(5);
      pull += (pullTarget - pull) * ease(2.5);
      darkMix += ((dark ? 1 : 0) - darkMix) * ease(6);

      syncMetal();
      var speed = numberOr(options.speed, DEFAULT_SPEED, 0, 4);
      gl.uniform2f(uResolution, canvas.width, canvas.height);
      gl.uniform1f(uTime, clock * speed * 3 + 11);
      gl.uniform1f(uCount, Math.round(numberOr(options.ribbons, DEFAULT_RIBBONS, 1, 5)));
      gl.uniform1f(uIridescence, numberOr(options.iridescence, DEFAULT_IRIDESCENCE, 0, 1));
      gl.uniform1f(uPull, options.interactive === false ? 0 : pull);
      gl.uniform1f(uDark, darkMix);
      gl.uniform2f(uMouse, mouse.x, mouse.y);
      gl.uniform2f(uLight, light.x, light.y);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES, 0, 6);

      if (flowing || settling()) raf = requestAnimationFrame(tick);
    }

    // Draws now: resizing cleared the canvas, and waiting for the next frame
    // would show it blank for one.
    function redraw() {
      if (destroyed || lost) return;
      cancelAnimationFrame(raf);
      raf = 0;
      last = performance.now();
      tick(last);
    }

    var ro = new ResizeObserver(function () {
      if (resize()) redraw();
    });
    if (canvas.parentElement) ro.observe(canvas.parentElement);

    var io = new IntersectionObserver(function (entries) {
      onScreen = entries[0] ? entries[0].isIntersecting : true;
      wake();
    });
    io.observe(canvas);

    function onVisibility() { wake(); }
    document.addEventListener("visibilitychange", onVisibility);
    function onReduce() { wake(); }
    mqReduce.addEventListener("change", onReduce);

    function syncTheme() {
      var next = resolveDark(options.theme || "auto");
      if (next === dark) return;
      dark = next;
      if (options.onThemeChange) options.onThemeChange(dark);
      wake();
    }
    var mo = new MutationObserver(syncTheme);
    mo.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme", "style"],
    });
    var mqDark = global.matchMedia("(prefers-color-scheme: dark)");
    mqDark.addEventListener("change", syncTheme);

    function onMove(e) {
      if (options.interactive === false) return;
      var rect = canvas.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      var x = (e.clientX - rect.left) / rect.width;
      var y = 1 - (e.clientY - rect.top) / rect.height;
      var inside = x >= 0 && x <= 1 && y >= 0 && y <= 1;
      pullTarget = inside ? 1 : 0;
      if (inside) {
        target.x = x;
        target.y = y;
        lightTarget.x = (x * 2 - 1) * 0.9;
        lightTarget.y = (y * 2 - 1) * 0.9;
      } else {
        lightTarget.x = REST_LIGHT.x;
        lightTarget.y = REST_LIGHT.y;
      }
      wake();
    }
    global.addEventListener("pointermove", onMove, { passive: true });

    // Without preventDefault the browser never restores a lost context.
    function onLost(e) {
      e.preventDefault();
      lost = true;
      cancelAnimationFrame(raf);
      raf = 0;
    }
    function onRestored() {
      if (destroyed || !initGl()) return;
      lost = false;
      f0Key = "";
      gl.viewport(0, 0, canvas.width, canvas.height);
      wake();
    }
    canvas.addEventListener("webglcontextlost", onLost);
    canvas.addEventListener("webglcontextrestored", onRestored);

    resize();
    wake();

    return {
      setOptions: function (next) {
        options = Object.assign({}, options, next);
        if (options.interactive === false) {
          pullTarget = 0;
          lightTarget.x = REST_LIGHT.x;
          lightTarget.y = REST_LIGHT.y;
        }
        syncTheme();
        wake();
      },
      destroy: function () {
        destroyed = true;
        cancelAnimationFrame(raf);
        raf = 0;
        ro.disconnect();
        io.disconnect();
        mo.disconnect();
        document.removeEventListener("visibilitychange", onVisibility);
        mqReduce.removeEventListener("change", onReduce);
        mqDark.removeEventListener("change", syncTheme);
        global.removeEventListener("pointermove", onMove);
        canvas.removeEventListener("webglcontextlost", onLost);
        canvas.removeEventListener("webglcontextrestored", onRestored);
        gl.deleteProgram(program);
        gl.deleteShader(vs);
        gl.deleteShader(fs);
        gl.deleteBuffer(buf);
        // Free the GPU context once the canvas has left the page; browsers cap
        // live contexts at about 16.
        var lose = gl.getExtension("WEBGL_lose_context");
        queueMicrotask(function () {
          if (!canvas.isConnected && !gl.isContextLost() && lose) lose.loseContext();
        });
      },
    };
  }

  global.createShaderMetal = createShaderMetal;
  global.SHADER_METALS = METALS;
})(window);

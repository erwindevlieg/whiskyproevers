/* Lazy-loaded regiokaart. Geodata: assets/regions.geojson (ScotchRegions + Natural Earth, vereenvoudigd). */
(function (global) {
  const GEO_URL = "assets/regions.geojson";
  let geoPromise = null;

  function loadGeo() {
    if (!geoPromise) {
      geoPromise = fetch(GEO_URL).then((res) => {
        if (!res.ok) throw new Error("regions");
        return res.json();
      });
    }
    return geoPromise;
  }

  function bboxOf(features) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    features.forEach((f) => eachCoord(f.geometry, (x, y) => {
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x > maxX) maxX = x;
      if (y > maxY) maxY = y;
    }));
    if (!isFinite(minX)) return null;
    return [minX, minY, maxX, maxY];
  }

  function eachCoord(geom, fn) {
    if (!geom) return;
    const polys = geom.type === "Polygon" ? [geom.coordinates]
      : geom.type === "MultiPolygon" ? geom.coordinates : [];
    polys.forEach((poly) => poly.forEach((ring) => ring.forEach((p) => fn(p[0], p[1]))));
  }

  function projector(bbox, width, height, pad) {
    const [minX, minY, maxX, maxY] = bbox;
    const midY = ((minY + maxY) / 2) * Math.PI / 180;
    const xW = (maxX - minX) * Math.max(0.3, Math.cos(midY));
    const yW = maxY - minY;
    const innerW = width - pad * 2;
    const innerH = height - pad * 2;
    const scale = Math.min(innerW / xW, innerH / yW);
    const ox = (width - xW * scale) / 2;
    const oy = (height - yW * scale) / 2;
    return function (x, y) {
      const px = ox + (x - minX) * Math.max(0.3, Math.cos(midY)) * scale;
      const py = height - (oy + (y - minY) * scale);
      return [px, py];
    };
  }

  function pathData(geom, project) {
    const polys = geom.type === "Polygon" ? [geom.coordinates]
      : geom.type === "MultiPolygon" ? geom.coordinates : [];
    const parts = [];
    polys.forEach((poly) => {
      poly.forEach((ring) => {
        if (!ring.length) return;
        const pts = ring.map((p) => project(p[0], p[1]));
        parts.push("M" + pts.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join("L") + "Z");
      });
    });
    return parts.join("");
  }

  function fillFor(count, max, active) {
    if (active) return null; // class handles it
    if (!count) return null;
    const t = Math.sqrt(count / max);
    const a = (0.38 + 0.54 * t).toFixed(2);
    return "hsl(var(--primary) / " + a + ")";
  }

  function svgEl(name, attrs) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.keys(attrs).forEach((k) => el.setAttribute(k, attrs[k]));
    return el;
  }

  function expandBBox(box, pad) {
    return [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad];
  }

  function drawFeatures(features, opts) {
    const width = opts.width || 640;
    const height = opts.height || 520;
    const counts = opts.counts || {};
    const active = opts.region || "";
    const clickable = opts.clickable !== false;
    const max = Math.max(1, ...features.map((f) => counts[f.properties.name] || 0));
    const bbox = opts.bbox || bboxOf(features);
    if (!bbox) return null;
    const pad = opts.pad || 12;
    const project = projector(bbox, width, height, pad);
    const svg = svgEl("svg", {
      viewBox: "0 0 " + width + " " + height,
      role: "img",
      "aria-label": opts.label || "Regiokaart"
    });
    features.forEach((f) => {
      const name = f.properties.name;
      const d = pathData(f.geometry, project);
      if (!d) return;
      const count = counts[name] || 0;
      const path = svgEl("path", { d: d, "class": "rm-region" });
      if (name === active) path.classList.add("is-active");
      else if (!count) path.classList.add("is-empty");
      if (count) {
        path.setAttribute("data-count", String(count));
        const fill = fillFor(count, max, name === active);
        if (fill) path.style.setProperty("--rm-fill", fill);
      }
      path.setAttribute("aria-label", name);
      if (clickable && count && typeof opts.onSelect === "function") {
        path.setAttribute("tabindex", "0");
        path.setAttribute("role", "button");
        const go = (ev) => { ev.preventDefault(); opts.onSelect(name); };
        path.addEventListener("click", go);
        path.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") go(ev);
        });
      }
      svg.appendChild(path);
    });
    return svg;
  }

  function mount(el, opts) {
    opts = opts || {};
    loadGeo().then((geo) => {
      const counts = opts.counts || {};
      const mode = opts.mode || "overview";
      const region = opts.region || "";
      const features = geo.features || [];
      const scotland = features.filter((f) => f.properties.group === "scotland");
      const world = features.filter((f) => f.properties.group === "world");
      el.hidden = false;
      el.innerHTML = "";

      if (mode === "detail") {
        const hit = features.find((f) => f.properties.name === region);
        if (!hit) { el.hidden = true; el.innerHTML = ""; return; }
        const isScot = hit.properties.group === "scotland";
        const group = isScot ? scotland : [hit];
        const local = bboxOf([hit]);
        const span = Math.max(local[2] - local[0], local[3] - local[1]);
        const tight = isScot && span < 2.4;
        const svg = drawFeatures(group, {
          width: isScot ? 520 : 360,
          height: isScot ? 400 : 260,
          counts: counts,
          region: region,
          clickable: false,
          label: region,
          bbox: tight ? expandBBox(local, Math.max(0.55, span * 1.6)) : null
        });
        if (svg) el.appendChild(svg);
        return;
      }

      const panel = document.createElement("div");
      panel.className = "rm-panel";
      const scotWrap = document.createElement("div");
      const scotSvg = drawFeatures(scotland, {
        width: 560,
        height: 480,
        counts: counts,
        region: region,
        onSelect: opts.onSelect,
        label: "Schotland"
      });
      if (scotSvg) scotWrap.appendChild(scotSvg);
      const worldWrap = document.createElement("div");
      worldWrap.className = "rm-world";
      const presentWorld = world.filter((f) => (counts[f.properties.name] || 0) > 0);
      presentWorld.forEach((f) => {
        const box = document.createElement("div");
        const svg = drawFeatures([f], {
          width: 220,
          height: 140,
          counts: counts,
          region: region,
          onSelect: opts.onSelect,
          pad: 10,
          label: f.properties.name
        });
        if (svg) {
          box.appendChild(svg);
          worldWrap.appendChild(box);
        }
      });
      panel.appendChild(scotWrap);
      if (presentWorld.length) panel.appendChild(worldWrap);
      el.appendChild(panel);
    }).catch(() => {
      el.hidden = true;
    });
  }

  global.RegionMap = { mount: mount };
})(window);

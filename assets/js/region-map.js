/* Lazy-loaded regiokaart voor de whiskypagina. Geodata: assets/regions.geojson (ScotchRegions + Natural Earth, vereenvoudigd). */
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

  function svgEl(name, attrs) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.keys(attrs).forEach((k) => el.setAttribute(k, attrs[k]));
    return el;
  }

  function expandBBox(box, pad) {
    return [box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad];
  }

  function focusBBox(local, isScot) {
    const spanX = local[2] - local[0];
    const spanY = local[3] - local[1];
    const span = Math.max(spanX, spanY);
    let pad;
    if (isScot) {
      // Kleine gebieden (Campbeltown, Islay) vullen de kaart, met net genoeg
      // omgeving. Grote regio's (Highland, Islands) krijgen een kleine rand.
      const targetSpan = Math.max(1.15, Math.min(span / 0.42, span + 2.4));
      pad = Math.max(0.28, (targetSpan - span) / 2);
    } else {
      pad = Math.max(span * 0.12, 0.15);
    }
    return expandBBox(local, pad);
  }

  function drawFeatures(features, opts) {
    const width = opts.width || 640;
    const height = opts.height || 520;
    const active = opts.region || "";
    const bbox = opts.bbox || bboxOf(features);
    if (!bbox) return null;
    const pad = opts.pad || 12;
    const project = projector(bbox, width, height, pad);
    const svg = svgEl("svg", {
      viewBox: "0 0 " + width + " " + height,
      role: "img",
      "aria-label": opts.label || "Regiokaart"
    });
    const rest = [];
    let activePath = null;
    features.forEach((f) => {
      const name = f.properties.name;
      const d = pathData(f.geometry, project);
      if (!d) return;
      const path = svgEl("path", { d: d, "class": "rm-region" });
      path.setAttribute("aria-label", name);
      if (name === active) {
        path.classList.add("is-active");
        activePath = path;
      } else {
        rest.push(path);
      }
    });
    rest.forEach((p) => svg.appendChild(p));
    if (activePath) svg.appendChild(activePath);
    return svg;
  }

  function mount(el, opts) {
    opts = opts || {};
    loadGeo().then((geo) => {
      const region = opts.region || "";
      const features = geo.features || [];
      const hit = features.find((f) => f.properties.name === region);
      if (!hit) {
        el.hidden = true;
        el.innerHTML = "";
        return;
      }
      const isScot = hit.properties.group === "scotland";
      const group = isScot
        ? features.filter((f) => f.properties.group === "scotland")
        : [hit];
      const local = bboxOf([hit]);
      const svg = drawFeatures(group, {
        width: isScot ? 520 : 360,
        height: isScot ? 400 : 260,
        region: region,
        label: region,
        bbox: local ? focusBBox(local, isScot) : null
      });
      el.hidden = false;
      el.innerHTML = "";
      if (svg) el.appendChild(svg);
      const caption = document.createElement("p");
      caption.className = "t-meta rm-caption";
      caption.textContent = region;
      el.appendChild(caption);
    }).catch(() => {
      el.hidden = true;
    });
  }

  global.RegionMap = { mount: mount };
})(window);

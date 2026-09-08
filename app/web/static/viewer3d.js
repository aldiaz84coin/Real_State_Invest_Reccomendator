/*
 * Vista 3D de la implantación, construida con la misma escena que el plano 2D.
 *
 * Todo llega resuelto desde el servidor en metros locales: aquí sólo hay
 * extrusión, materiales e iluminación. Ninguna decisión de diseño se toma en
 * el navegador.
 *
 * El realismo no viene de cambiar de motor sino de tres cosas que faltaban:
 * materiales con textura en vez de colores planos, luz solar con la posición
 * real del sol para esa latitud, y volumen construido de verdad (aleros,
 * carpinterías, barandillas) en lugar de cajas.
 */
(function () {
  'use strict';

  // --- texturas procedurales -------------------------------------------
  // Se generan en el navegador para no depender de ficheros externos, que
  // además chocarían con la política de contenidos.

  function makeCanvas(size) {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = size;
    return canvas;
  }

  function noiseTexture(THREE, base, spread, size, repeat) {
    const canvas = makeCanvas(size || 256);
    const ctx = canvas.getContext('2d');
    const image = ctx.createImageData(canvas.width, canvas.height);
    for (let i = 0; i < image.data.length; i += 4) {
      const variation = (Math.random() - 0.5) * spread;
      image.data[i] = Math.max(0, Math.min(255, base[0] + variation));
      image.data[i + 1] = Math.max(0, Math.min(255, base[1] + variation));
      image.data[i + 2] = Math.max(0, Math.min(255, base[2] + variation));
      image.data[i + 3] = 255;
    }
    ctx.putImageData(image, 0, 0);
    const texture = new THREE.CanvasTexture(canvas);
    texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
    texture.repeat.set(repeat || 24, repeat || 24);
    return texture;
  }

  function grassTexture(THREE) {
    const canvas = makeCanvas(256);
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#7d9b57';
    ctx.fillRect(0, 0, 256, 256);
    // Matas sueltas: rompen el verde plano, que es lo que delata una maqueta.
    for (let i = 0; i < 2600; i++) {
      const tone = 70 + Math.random() * 60;
      ctx.fillStyle = `rgba(${tone + 40}, ${tone + 70}, ${tone + 20}, .5)`;
      ctx.fillRect(Math.random() * 256, Math.random() * 256, 2, 3 + Math.random() * 4);
    }
    const texture = new THREE.CanvasTexture(canvas);
    texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
    texture.repeat.set(30, 30);
    return texture;
  }

  function deckTexture(THREE) {
    const canvas = makeCanvas(256);
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#b98d5c';
    ctx.fillRect(0, 0, 256, 256);
    for (let y = 0; y < 256; y += 32) {
      const tone = 150 + Math.random() * 40;
      ctx.fillStyle = `rgb(${tone + 30}, ${tone - 15}, ${tone - 60})`;
      ctx.fillRect(0, y, 256, 30);
      ctx.strokeStyle = 'rgba(90,60,35,.55)';
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(256, y); ctx.stroke();
    }
    const texture = new THREE.CanvasTexture(canvas);
    texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
    texture.repeat.set(4, 2);
    return texture;
  }

  // --- geometría auxiliar -----------------------------------------------

  function ringToShape(THREE, ring) {
    const shape = new THREE.Shape();
    ring.forEach(function (p, i) {
      if (i === 0) shape.moveTo(p[0], p[1]); else shape.lineTo(p[0], p[1]);
    });
    shape.closePath();
    return shape;
  }

  function centroidOf(ring) {
    return [
      ring.reduce(function (s, p) { return s + p[0]; }, 0) / ring.length,
      ring.reduce(function (s, p) { return s + p[1]; }, 0) / ring.length
    ];
  }

  function scaleRing(ring, factor) {
    const c = centroidOf(ring);
    return ring.map(function (p) {
      return [c[0] + (p[0] - c[0]) * factor, c[1] + (p[1] - c[1]) * factor];
    });
  }

  /* Posición del sol para una latitud y una hora dadas.
   * Con el sol en un sitio arbitrario las sombras no dicen nada; con la
   * geometría real se ve si la terraza queda soleada, que es justo el criterio
   * con el que el servidor decidió dónde colocar la casa. */
  function sunDirection(latitude, dayOfYear, hour) {
    const rad = Math.PI / 180;
    const declination = 23.44 * rad * Math.sin(2 * Math.PI * (284 + dayOfYear) / 365);
    const hourAngle = (hour - 12) * 15 * rad;
    const lat = latitude * rad;
    const altitude = Math.asin(
      Math.sin(lat) * Math.sin(declination) +
      Math.cos(lat) * Math.cos(declination) * Math.cos(hourAngle)
    );
    let azimuth = Math.atan2(
      Math.sin(hourAngle),
      Math.cos(hourAngle) * Math.sin(lat) - Math.tan(declination) * Math.cos(lat)
    );
    azimuth += Math.PI; // 0 = norte
    // Mundo: norte en -Z, este en +X, arriba +Y.
    return {
      x: Math.sin(azimuth) * Math.cos(altitude),
      y: Math.max(Math.sin(altitude), 0.12),
      z: -Math.cos(azimuth) * Math.cos(altitude),
      altitude: altitude
    };
  }

  function buildScene(container, plan) {
    const THREE = window.THREE;
    if (!THREE) {
      container.innerHTML = '<p class="note" style="padding:20px">No se pudo cargar Three.js.</p>';
      return;
    }

    const width = container.clientWidth;
    const height = container.clientHeight || 520;

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    // Sin esto los materiales salen lavados y con los colores mal saturados.
    if (THREE.sRGBEncoding !== undefined) renderer.outputEncoding = THREE.sRGBEncoding;
    if (THREE.ACESFilmicToneMapping !== undefined) {
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = 1.05;
    }
    container.innerHTML = '';
    container.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 4000);

    // --- cielo ----------------------------------------------------------
    const skyGeometry = new THREE.SphereGeometry(1200, 32, 16);
    const skyMaterial = new THREE.ShaderMaterial({
      side: THREE.BackSide,
      uniforms: {
        top: { value: new THREE.Color(0x3f76b5) },
        bottom: { value: new THREE.Color(0xdfe9f0) },
        offset: { value: 120 }
      },
      vertexShader:
        'varying vec3 vPos; void main(){ vPos = position; ' +
        'gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',
      fragmentShader:
        'uniform vec3 top; uniform vec3 bottom; uniform float offset; varying vec3 vPos;' +
        'void main(){ float h = normalize(vPos + vec3(0.0, offset, 0.0)).y;' +
        'gl_FragColor = vec4(mix(bottom, top, max(pow(max(h,0.0), 0.7), 0.0)), 1.0); }'
    });
    scene.add(new THREE.Mesh(skyGeometry, skyMaterial));
    scene.fog = new THREE.Fog(0xcfdce8, 140, 620);

    // --- luz ------------------------------------------------------------
    const latitude = (plan.origin && plan.origin.lat) || 40;
    const sun = sunDirection(latitude, 172, 16);   // solsticio de verano, tarde

    scene.add(new THREE.HemisphereLight(0xbcd6ef, 0x5d6b45, 0.75));
    const sunLight = new THREE.DirectionalLight(0xfff0d6, 2.1);
    sunLight.position.set(sun.x * 90, sun.y * 90, sun.z * 90);
    sunLight.castShadow = true;
    sunLight.shadow.mapSize.set(2048, 2048);
    sunLight.shadow.bias = -0.0005;
    const shadowSpan = 90;
    sunLight.shadow.camera.left = -shadowSpan;
    sunLight.shadow.camera.right = shadowSpan;
    sunLight.shadow.camera.top = shadowSpan;
    sunLight.shadow.camera.bottom = -shadowSpan;
    sunLight.shadow.camera.far = 400;
    scene.add(sunLight);
    // Relleno frío desde el lado opuesto: evita sombras completamente negras.
    const fill = new THREE.DirectionalLight(0xaecbe8, 0.35);
    fill.position.set(-sun.x * 60, 40, -sun.z * 60);
    scene.add(fill);

    const group = new THREE.Group();
    scene.add(group);

    function addSlab(ring, thickness, options) {
      if (!ring || ring.length < 3) return null;
      const opts = options || {};
      const geometry = new THREE.ExtrudeGeometry(ringToShape(THREE, ring), {
        depth: thickness, bevelEnabled: false
      });
      geometry.rotateX(-Math.PI / 2);   // la extrusión crece en +Z; se tumba
      const material = new THREE.MeshStandardMaterial({
        color: opts.color === undefined ? 0xffffff : opts.color,
        map: opts.map || null,
        roughness: opts.roughness === undefined ? 0.85 : opts.roughness,
        metalness: opts.metalness || 0,
        transparent: opts.opacity !== undefined,
        opacity: opts.opacity === undefined ? 1 : opts.opacity
      });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.y = opts.y || 0;
      mesh.receiveShadow = true;
      mesh.castShadow = opts.castShadow !== false;
      group.add(mesh);
      return mesh;
    }

    const parcel = (plan.parcel && plan.parcel.meters) || [];
    if (parcel.length < 3) return;

    // --- terreno ---------------------------------------------------------
    const centre = centroidOf(parcel);
    const terrain = new THREE.Mesh(
      new THREE.PlaneGeometry(900, 900),
      new THREE.MeshStandardMaterial({
        color: 0x8fa86a, map: noiseTexture(THREE, [128, 145, 96], 26, 128, 90), roughness: 1
      })
    );
    terrain.rotation.x = -Math.PI / 2;
    terrain.position.set(centre[0], -0.55, -centre[1]);
    terrain.receiveShadow = true;
    scene.add(terrain);

    addSlab(parcel, 0.5, {
      color: 0xffffff, map: grassTexture(THREE), roughness: 1, y: -0.5, castShadow: false
    });
    if (plan.buildable && plan.buildable.meters) {
      addSlab(plan.buildable.meters, 0.02, {
        color: 0xf0f4e2, roughness: 1, opacity: 0.35, y: 0.01, castShadow: false
      });
    }

    // --- pavimentos ------------------------------------------------------
    if (plan.parking && plan.parking.meters) {
      addSlab(plan.parking.meters, 0.1, {
        color: 0x9a9a95, map: noiseTexture(THREE, [150, 148, 140], 40, 128, 6),
        roughness: 1, y: 0.02, castShadow: false
      });
    }
    if (plan.driveway_meters && plan.driveway_meters.length > 1) {
      const points = plan.driveway_meters.map(function (p) {
        return new THREE.Vector3(p[0], 0.07, -p[1]);
      });
      const path = new THREE.Mesh(
        new THREE.TubeGeometry(new THREE.CatmullRomCurve3(points), 48, 1.6, 8, false),
        new THREE.MeshStandardMaterial({
          color: 0xbfae92, map: noiseTexture(THREE, [190, 175, 148], 34, 128, 8), roughness: 1
        })
      );
      path.scale.y = 0.05;
      path.receiveShadow = true;
      group.add(path);
    }

    // --- terraza con barandilla -------------------------------------------
    if (plan.terrace && plan.terrace.meters) {
      addSlab(plan.terrace.meters, 0.3, {
        color: 0xffffff, map: deckTexture(THREE), roughness: 0.75, y: 0.04
      });
      const ring = plan.terrace.meters;
      for (let i = 0; i < ring.length; i++) {
        const a = ring[i];
        const b = ring[(i + 1) % ring.length];
        const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
        if (length < 0.5) continue;
        const rail = new THREE.Mesh(
          new THREE.BoxGeometry(length, 0.06, 0.06),
          new THREE.MeshStandardMaterial({ color: 0x5c5044, roughness: 0.6, metalness: 0.3 })
        );
        rail.position.set((a[0] + b[0]) / 2, 1.0, -(a[1] + b[1]) / 2);
        rail.rotation.y = -Math.atan2(b[1] - a[1], b[0] - a[0]);
        rail.castShadow = true;
        group.add(rail);
      }
    }

    // --- piscina ------------------------------------------------------------
    if (plan.pool && plan.pool.meters) {
      addSlab(scaleRing(plan.pool.meters, 1.22), 0.12, {
        color: 0xe7e2d5, roughness: 0.9, y: 0.02, castShadow: false
      });
      addSlab(plan.pool.meters, 0.9, { color: 0x1d6f92, roughness: 0.25, y: -0.85, castShadow: false });
      addSlab(plan.pool.meters, 0.02, {
        color: 0x35a7d4, roughness: 0.06, metalness: 0.35, opacity: 0.78, y: 0.05, castShadow: false
      });
    }

    // --- vivienda -----------------------------------------------------------
    const house = plan.house || {};
    if (house.meters && house.meters.length >= 3) {
      const wallHeight = house.height_m || 2.8;
      addSlab(house.meters, wallHeight, {
        color: 0xeee7db, map: noiseTexture(THREE, [235, 229, 215], 12, 128, 3), roughness: 0.9
      });
      // Zócalo: separa visualmente la casa del césped.
      addSlab(scaleRing(house.meters, 1.04), 0.35, { color: 0x8d8378, roughness: 1, y: 0 });

      // Cubierta plana con alero y peto, como la de un módulo prefabricado.
      addSlab(scaleRing(house.meters, 1.13), 0.22, {
        color: 0x413d39, roughness: 0.7, y: wallHeight
      });
      addSlab(scaleRing(house.meters, 1.05), 0.28, {
        color: 0x4c4842, roughness: 0.8, y: wallHeight + 0.22
      });

      // Ventanal continuo con carpintería, en vez de una banda pintada.
      const bandY = wallHeight * 0.40;
      const bandH = wallHeight * 0.42;
      addSlab(scaleRing(house.meters, 1.012), bandH, {
        color: 0x1e3a49, roughness: 0.08, metalness: 0.55, opacity: 0.86, y: bandY
      });
      addSlab(scaleRing(house.meters, 1.022), 0.07, { color: 0x2f2b28, roughness: 0.5, y: bandY });
      addSlab(scaleRing(house.meters, 1.022), 0.07, {
        color: 0x2f2b28, roughness: 0.5, y: bandY + bandH
      });

      // Puerta en la fachada sur, donde el servidor situó la entrada.
      if (house.door && plan.origin) {
        const south = house.meters.reduce(function (best, p) {
          return p[1] < best[1] ? p : best;
        }, house.meters[0]);
        const door = new THREE.Mesh(
          new THREE.BoxGeometry(0.95, wallHeight * 0.72, 0.12),
          new THREE.MeshStandardMaterial({ color: 0x7d4a2b, roughness: 0.55 })
        );
        const c = centroidOf(house.meters);
        door.position.set(
          c[0] + (south[0] - c[0]) * 0.45,
          wallHeight * 0.36,
          -(c[1] + (south[1] - c[1]) * 1.02)
        );
        door.castShadow = true;
        group.add(door);
      }
    }

    // --- arbolado ------------------------------------------------------------
    (plan.trees || []).forEach(function (tree, index) {
      const trunkHeight = tree.height_m * 0.42;
      const trunk = new THREE.Mesh(
        new THREE.CylinderGeometry(0.13, 0.2, trunkHeight, 7),
        new THREE.MeshStandardMaterial({ color: 0x6b4a30, roughness: 1 })
      );
      trunk.position.set(tree.x, trunkHeight / 2, -tree.y);
      trunk.castShadow = true;
      group.add(trunk);

      // Dos especies alternas: una masa vegetal uniforme parece de maqueta.
      const conifera = index % 3 === 0;
      const crownMaterial = new THREE.MeshStandardMaterial({
        color: conifera ? 0x3f6b3a : 0x5f8f45, roughness: 0.95, flatShading: true
      });
      let crown;
      if (conifera) {
        crown = new THREE.Mesh(
          new THREE.ConeGeometry(tree.radius_m, tree.height_m * 0.95, 8), crownMaterial);
        crown.position.set(tree.x, trunkHeight + tree.height_m * 0.42, -tree.y);
      } else {
        crown = new THREE.Mesh(
          new THREE.SphereGeometry(tree.radius_m, 9, 7), crownMaterial);
        crown.position.set(tree.x, trunkHeight + tree.radius_m * 0.85, -tree.y);
        crown.scale.set(1, 1.15, 1);
      }
      crown.castShadow = true;
      group.add(crown);
    });

    // --- encuadre y órbita ------------------------------------------------------
    const box = new THREE.Box3().setFromObject(group);
    const size = box.getSize(new THREE.Vector3());
    const target = box.getCenter(new THREE.Vector3());
    const radius = Math.max(size.x, size.z) || 40;

    const state = { azimuth: -0.9, polar: 1.12, distance: radius * 1.6 };
    const wanted = { azimuth: state.azimuth, polar: state.polar, distance: state.distance };

    function applyCamera() {
      // Amortiguado: el giro con inercia se lee mucho mejor que el salto seco.
      state.azimuth += (wanted.azimuth - state.azimuth) * 0.12;
      state.polar += (wanted.polar - state.polar) * 0.12;
      state.distance += (wanted.distance - state.distance) * 0.12;
      camera.position.set(
        target.x + state.distance * Math.sin(state.polar) * Math.cos(state.azimuth),
        target.y + state.distance * Math.cos(state.polar),
        target.z + state.distance * Math.sin(state.polar) * Math.sin(state.azimuth)
      );
      camera.lookAt(target);
    }
    applyCamera();

    let dragging = false, lastX = 0, lastY = 0;
    const canvas = renderer.domElement;
    canvas.style.cursor = 'grab';
    canvas.addEventListener('pointerdown', function (event) {
      dragging = true; lastX = event.clientX; lastY = event.clientY;
      canvas.style.cursor = 'grabbing'; canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener('pointermove', function (event) {
      if (!dragging) return;
      wanted.azimuth -= (event.clientX - lastX) * 0.006;
      // Se limita la vertical para no atravesar el suelo ni mirar desde debajo.
      wanted.polar = Math.max(0.18, Math.min(1.45, wanted.polar - (event.clientY - lastY) * 0.006));
      lastX = event.clientX; lastY = event.clientY;
    });
    ['pointerup', 'pointercancel'].forEach(function (type) {
      canvas.addEventListener(type, function () { dragging = false; canvas.style.cursor = 'grab'; });
    });
    canvas.addEventListener('wheel', function (event) {
      event.preventDefault();
      wanted.distance = Math.max(radius * 0.45, Math.min(radius * 4,
        wanted.distance * (event.deltaY > 0 ? 1.12 : 0.89)));
    }, { passive: false });

    window.addEventListener('resize', function () {
      const w = container.clientWidth;
      if (!w) return;
      camera.aspect = w / height;
      camera.updateProjectionMatrix();
      renderer.setSize(w, height);
    });

    (function animate() {
      requestAnimationFrame(animate);
      applyCamera();
      renderer.render(scene, camera);
    })();
  }

  window.initSiteViewer3D = function (containerId, plan) {
    const container = document.getElementById(containerId);
    if (!container || !plan || !plan.parcel) return;
    try {
      buildScene(container, plan);
    } catch (error) {
      container.innerHTML =
        '<p class="note" style="padding:20px">No se pudo dibujar la vista 3D: ' +
        error.message + '</p>';
    }
  };
})();

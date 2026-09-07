/*
 * Vista 3D de la implantación, construida con la misma escena que el plano 2D.
 * Todo llega ya resuelto desde el servidor en metros locales, así que aquí solo
 * hay extrusión y montaje: ninguna decisión de diseño se toma en el navegador.
 */
(function () {
  'use strict';

  function buildScene(container, plan) {
    if (!window.THREE) {
      container.innerHTML = '<p class="note" style="padding:20px">No se ha podido cargar Three.js.</p>';
      return;
    }
    const THREE = window.THREE;
    const width = container.clientWidth;
    const height = container.clientHeight || 520;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xdce8f0);
    scene.fog = new THREE.Fog(0xdce8f0, 90, 320);

    const camera = new THREE.PerspectiveCamera(48, width / height, 0.1, 2000);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    container.innerHTML = '';
    container.appendChild(renderer.domElement);

    // --- iluminación: sol del sur, como en el criterio de implantación ---
    scene.add(new THREE.HemisphereLight(0xbfd9ee, 0x6b7a55, 0.85));
    const sun = new THREE.DirectionalLight(0xfff3dd, 1.5);
    sun.position.set(-35, 55, -45);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    sun.shadow.camera.left = -80; sun.shadow.camera.right = 80;
    sun.shadow.camera.top = 80; sun.shadow.camera.bottom = -80;
    scene.add(sun);

    const group = new THREE.Group();
    scene.add(group);

    function shapeFrom(ring) {
      const shape = new THREE.Shape();
      ring.forEach(function (p, i) {
        if (i === 0) shape.moveTo(p[0], p[1]);
        else shape.lineTo(p[0], p[1]);
      });
      shape.closePath();
      return shape;
    }

    function addSlab(ring, thickness, color, y, opts) {
      if (!ring || ring.length < 3) return null;
      const options = opts || {};
      const geometry = new THREE.ExtrudeGeometry(shapeFrom(ring), {
        depth: thickness, bevelEnabled: false
      });
      // La extrusión crece en +Z, así que se tumba el sólido sobre el plano XZ.
      geometry.rotateX(-Math.PI / 2);
      const material = new THREE.MeshStandardMaterial({
        color: color,
        roughness: options.roughness === undefined ? 0.9 : options.roughness,
        metalness: 0,
        transparent: options.opacity !== undefined,
        opacity: options.opacity === undefined ? 1 : options.opacity
      });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.y = y || 0;
      mesh.receiveShadow = true;
      mesh.castShadow = !!options.castShadow;
      group.add(mesh);
      return mesh;
    }

    // --- terreno, área edificable y pavimentos ---
    const parcel = (plan.parcel && plan.parcel.meters) || [];
    addSlab(parcel, 0.4, 0x87a865, -0.4);
    if (plan.buildable && plan.buildable.meters) {
      addSlab(plan.buildable.meters, 0.03, 0xa8c98a, 0.01, { opacity: 0.5 });
    }
    if (plan.parking && plan.parking.meters) addSlab(plan.parking.meters, 0.08, 0x9a9d9f, 0.02);
    if (plan.terrace && plan.terrace.meters) {
      addSlab(plan.terrace.meters, 0.28, 0xd8bd92, 0.02, { castShadow: true });
    }
    if (plan.pool && plan.pool.meters) {
      addSlab(plan.pool.meters, 0.12, 0xe8e4d8, -0.05);
      addSlab(plan.pool.meters, 0.05, 0x2f9fd0, 0.06, { opacity: 0.82, roughness: 0.15 });
    }

    // --- camino de acceso ---
    if (plan.driveway_meters && plan.driveway_meters.length > 1) {
      const points = plan.driveway_meters.map(function (p) {
        return new THREE.Vector3(p[0], 0.06, -p[1]);
      });
      const curve = new THREE.CatmullRomCurve3(points);
      const tube = new THREE.Mesh(
        new THREE.TubeGeometry(curve, 40, 1.5, 6, false),
        new THREE.MeshStandardMaterial({ color: 0xbCA98a, roughness: 1 })
      );
      tube.scale.y = 0.06;
      tube.position.y = 0.05;
      tube.receiveShadow = true;
      group.add(tube);
    }

    // --- vivienda ---
    const house = plan.house || {};
    if (house.meters && house.meters.length >= 3) {
      const wallHeight = house.height_m || 2.8;
      addSlab(house.meters, wallHeight, 0xe8e0d2, 0, { castShadow: true, roughness: 0.75 });

      // Cubierta ligeramente volada, que es lo propio de un módulo prefabricado.
      const centerX = house.meters.reduce(function (s, p) { return s + p[0]; }, 0) / house.meters.length;
      const centerY = house.meters.reduce(function (s, p) { return s + p[1]; }, 0) / house.meters.length;
      const roofRing = house.meters.map(function (p) {
        return [centerX + (p[0] - centerX) * 1.12, centerY + (p[1] - centerY) * 1.18];
      });
      addSlab(roofRing, 0.28, 0x4a4642, wallHeight, { castShadow: true, roughness: 0.6 });

      // Banda de ventanal continuo, apenas volada sobre el plano de fachada.
      // Se agranda el anillo respecto al centro de la casa en lugar de escalar
      // la malla, porque la geometría está en coordenadas de parcela y un
      // escalado la desplazaría en vez de engordarla.
      const bandRing = house.meters.map(function (p) {
        return [centerX + (p[0] - centerX) * 1.015, centerY + (p[1] - centerY) * 1.05];
      });
      addSlab(bandRing, 1.25, 0x2c4a58, wallHeight * 0.42, { roughness: 0.08, opacity: 0.92 });
    }

    // --- arbolado ---
    (plan.trees || []).forEach(function (tree) {
      const trunk = new THREE.Mesh(
        new THREE.CylinderGeometry(0.16, 0.22, tree.height_m * 0.45, 6),
        new THREE.MeshStandardMaterial({ color: 0x6b4f34, roughness: 1 })
      );
      trunk.position.set(tree.x, tree.height_m * 0.22, -tree.y);
      trunk.castShadow = true;
      group.add(trunk);

      const crown = new THREE.Mesh(
        new THREE.SphereGeometry(tree.radius_m, 10, 8),
        new THREE.MeshStandardMaterial({ color: 0x5d8f4a, roughness: 0.95, flatShading: true })
      );
      crown.position.set(tree.x, tree.height_m * 0.62, -tree.y);
      crown.scale.y = 1.25;
      crown.castShadow = true;
      group.add(crown);
    });

    // El norte del terreno (+Y local) queda en -Z del mundo: lo fija ya la
    // rotación de la extrusión, y los árboles y el camino usan ese mismo
    // criterio al colocarse. No hace falta voltear el grupo.

    // --- encuadre y órbita ---
    const box = new THREE.Box3().setFromObject(group);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(size.x, size.z) || 40;

    const state = { azimuth: -0.75, polar: 1.02, distance: radius * 1.75 };

    function updateCamera() {
      state.polar = Math.max(0.18, Math.min(1.45, state.polar));
      state.distance = Math.max(radius * 0.6, Math.min(radius * 5, state.distance));
      camera.position.set(
        center.x + state.distance * Math.sin(state.polar) * Math.cos(state.azimuth),
        center.y + state.distance * Math.cos(state.polar),
        center.z + state.distance * Math.sin(state.polar) * Math.sin(state.azimuth)
      );
      camera.lookAt(center);
    }
    updateCamera();

    let dragging = false, lastX = 0, lastY = 0;
    const canvas = renderer.domElement;
    canvas.style.cursor = 'grab';
    canvas.addEventListener('pointerdown', function (event) {
      dragging = true; lastX = event.clientX; lastY = event.clientY;
      canvas.style.cursor = 'grabbing'; canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener('pointermove', function (event) {
      if (!dragging) return;
      state.azimuth -= (event.clientX - lastX) * 0.006;
      state.polar -= (event.clientY - lastY) * 0.006;
      lastX = event.clientX; lastY = event.clientY;
      updateCamera();
    });
    ['pointerup', 'pointercancel'].forEach(function (type) {
      canvas.addEventListener(type, function () { dragging = false; canvas.style.cursor = 'grab'; });
    });
    canvas.addEventListener('wheel', function (event) {
      event.preventDefault();
      state.distance *= event.deltaY > 0 ? 1.1 : 0.9;
      updateCamera();
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
        '<p class="note" style="padding:20px">No se ha podido dibujar la vista 3D: ' +
        error.message + '</p>';
    }
  };
})();

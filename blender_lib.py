import bpy
import bmesh
import math
import random
from mathutils import Vector, Matrix, Euler, Quaternion

# ==============================================================================
# 1. SCENE RESET & COLLECTION MANAGEMENT (Context-Free)
# ==============================================================================
def clear_scene():
    """Safely removes all objects, meshes, materials, and orphan data without bpy.ops."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for block_collection in (
        bpy.data.meshes, bpy.data.materials, bpy.data.textures,
        bpy.data.images, bpy.data.curves, bpy.data.lights, bpy.data.cameras
    ):
        for block in list(block_collection):
            block_collection.remove(block)

def get_or_create_collection(name, parent_collection=None):
    """Creates or retrieves a collection for clean scene hierarchy."""
    if name in bpy.data.collections:
        return bpy.data.collections[name]
    col = bpy.data.collections.new(name)
    if parent_collection is None:
        bpy.context.scene.collection.children.link(col)
    else:
        parent_collection.children.link(col)
    return col

def link_object(obj, collection=None):
    """Links an object exclusively to the target collection (or scene root)."""
    for col in list(obj.users_collection):
        col.objects.unlink(obj)
    target = collection if collection else bpy.context.scene.collection
    target.objects.link(obj)
    return obj

# ==============================================================================
# 2. CONTEXT-FREE PRIMITIVE GENERATORS (Pure BMesh)
# ==============================================================================
def create_box(name="Box", size=(1.0, 1.0, 1.0), location=(0, 0, 0), rotation=(0, 0, 0), segments=(1, 1, 1), collection=None):
    """Creates a box with customizable dimensions and optional subdivisions."""
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    
    # Scale vertices to exact dimensions
    bmesh.ops.scale(bm, vec=Vector(size), verts=bm.verts)
    
    # Subdivide if requested
    max_cuts = max(segments) - 1
    if max_cuts > 0:
        bmesh.ops.subdivide_edges(bm, edges=bm.edges, cuts=max_cuts, use_grid_fill=True)
        
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    
    obj = bpy.data.objects.new(name, mesh)
    obj.location = Vector(location)
    obj.rotation_euler = Euler([math.radians(a) for a in rotation], 'XYZ')
    return link_object(obj, collection)

def create_cylinder(name="Cylinder", radius1=0.5, radius2=0.5, depth=1.0, vertices=32, location=(0, 0, 0), rotation=(0, 0, 0), cap_ends=True, collection=None):
    """Creates a cylinder or cone (set radius2 != radius1 for cone/frustum)."""
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cone(
        bm,
        cap_ends=cap_ends,
        cap_tris=False,
        segments=vertices,
        radius1=radius1,
        radius2=radius2,
        depth=depth
    )
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    
    obj = bpy.data.objects.new(name, mesh)
    obj.location = Vector(location)
    obj.rotation_euler = Euler([math.radians(a) for a in rotation], 'XYZ')
    return link_object(obj, collection)

def create_sphere(name="Sphere", radius=0.5, u_segments=32, v_segments=16, location=(0, 0, 0), scale=(1, 1, 1), collection=None):
    """Creates a UV Sphere (or ellipsoid via scale parameter)."""
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=u_segments, v_segments=v_segments, radius=radius)
    if scale != (1, 1, 1):
        bmesh.ops.scale(bm, vec=Vector(scale), verts=bm.verts)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    
    obj = bpy.data.objects.new(name, mesh)
    obj.location = Vector(location)
    return link_object(obj, collection)

def create_icosphere(name="Icosphere", radius=0.5, subdivisions=3, location=(0, 0, 0), scale=(1, 1, 1), collection=None):
    """Creates an Icosphere (ideal for organic distortion, rocks, foliage)."""
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=subdivisions, radius=radius)
    if scale != (1, 1, 1):
        bmesh.ops.scale(bm, vec=Vector(scale), verts=bm.verts)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    
    obj = bpy.data.objects.new(name, mesh)
    obj.location = Vector(location)
    return link_object(obj, collection)

def create_torus(name="Torus", major_radius=1.0, minor_radius=0.25, major_seg=48, minor_seg=16, location=(0, 0, 0), rotation=(0, 0, 0), collection=None):
    """Creates a procedural Torus without bpy.ops."""
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    
    verts = []
    for i in range(major_seg):
        theta = 2.0 * math.pi * i / major_seg
        ring = []
        for j in range(minor_seg):
            phi = 2.0 * math.pi * j / minor_seg
            r = major_radius + minor_radius * math.cos(phi)
            x = r * math.cos(theta)
            y = r * math.sin(theta)
            z = minor_radius * math.sin(phi)
            ring.append(bm.verts.new((x, y, z)))
        verts.append(ring)
        
    for i in range(major_seg):
        next_i = (i + 1) % major_seg
        for j in range(minor_seg):
            next_j = (j + 1) % minor_seg
            bm.faces.new((
                verts[i][j],
                verts[next_i][j],
                verts[next_i][next_j],
                verts[i][next_j]
            ))
            
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    
    obj = bpy.data.objects.new(name, mesh)
    obj.location = Vector(location)
    obj.rotation_euler = Euler([math.radians(a) for a in rotation], 'XYZ')
    return link_object(obj, collection)

# ==============================================================================
# 3. SHADING, ORIGINS, & TRANSFORMS
# ==============================================================================
def set_smooth_shading(obj, auto_smooth_angle=35.0):
    """Applies smooth shading safely across Blender 3.x and 4.x+."""
    if obj.type != 'MESH':
        return
    mesh = obj.data
    for poly in mesh.polygons:
        poly.use_smooth = True
    # Blender 3.x compatibility
    if hasattr(mesh, "use_auto_smooth"):
        mesh.use_auto_smooth = True
        mesh.auto_smooth_angle = math.radians(auto_smooth_angle)

def set_origin_to_bottom(obj):
    """Moves the object's origin to the bottom-center of its bounding box."""
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    if not bm.verts:
        bm.free()
        return
    xs = [v.co.x for v in bm.verts]
    ys = [v.co.y for v in bm.verts]
    zs = [v.co.z for v in bm.verts]
    bottom_center = Vector(((min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5, min(zs)))
    bmesh.ops.translate(bm, vec=-bottom_center, verts=bm.verts)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    obj.location += obj.matrix_world.to_3x3() @ bottom_center

def parent_objects(child, parent, keep_transform=True):
    """Parents child to parent while preserving world transform."""
    world_mat = child.matrix_world.copy()
    child.parent = parent
    if keep_transform:
        child.matrix_parent_inverse = parent.matrix_world.inverted()
        child.matrix_world = world_mat


def create_lathe_object(name, profile_points_xz, steps=48, location=(0, 0, 0), collection=None):
    """
    profile_points_xz: list of (radius_x, height_z) tuples from bottom to top.
    Example goblet: [(0.3, 0.0), (0.3, 0.04), (0.04, 0.08), (0.04, 0.4), (0.28, 0.55), (0.3, 0.8), (0.27, 0.8), (0.25, 0.56), (0.0, 0.45)]
    """
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    
    prev_v = None
    profile_verts = []
    profile_edges = []
    for r, z in profile_points_xz:
        v = bm.verts.new((r, 0.0, z))
        profile_verts.append(v)
        if prev_v is not None:
            profile_edges.append(bm.edges.new((prev_v, v)))
        prev_v = v
        
    bmesh.ops.spin(
        bm,
        geom=profile_verts + profile_edges,
        cent=(0.0, 0.0, 0.0),
        axis=(0.0, 0.0, 1.0),
        angle=2.0 * math.pi,
        steps=steps,
        use_merge=True
    )
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    
    obj = bpy.data.objects.new(name, mesh)
    obj.location = Vector(location)
    set_smooth_shading(obj)
    return link_object(obj, collection)


def create_lofted_mesh(name, cross_sections, closed_ring=True, cap_ends=True, location=(0, 0, 0), collection=None):
    """
    cross_sections: list of rings, where each ring is a list of N (x, y, z) tuples.
    All rings must have the exact same vertex count N.
    """
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    
    rings = []
    for section in cross_sections:
        ring = [bm.verts.new(pt) for pt in section]
        rings.append(ring)
        
    num_rings = len(rings)
    ring_len = len(rings[0])
    
    for r in range(num_rings - 1):
        for i in range(ring_len if closed_ring else ring_len - 1):
            next_i = (i + 1) % ring_len
            bm.faces.new((
                rings[r][i],
                rings[r][next_i],
                rings[r + 1][next_i],
                rings[r + 1][i]
            ))
            
    if cap_ends and closed_ring and ring_len >= 3:
        bm.faces.new(reversed(rings[0]))
        bm.faces.new(rings[-1])
        
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    
    obj = bpy.data.objects.new(name, mesh)
    obj.location = Vector(location)
    set_smooth_shading(obj)
    return link_object(obj, collection)


def create_tube_curve(name, points, radii=None, bevel_depth=0.05, resolution=12, cyclic=False, collection=None):
    """
    points: list of (x, y, z) coordinates.
    radii: optional list of float multipliers per point (e.g., [1.0, 0.8, 0.4, 0.05] for a tapered horn/tentacle).
    """
    curve_data = bpy.data.curves.new(name=name, type='CURVE')
    curve_data.dimensions = '3D'
    curve_data.resolution_u = resolution
    curve_data.bevel_depth = bevel_depth
    curve_data.bevel_resolution = 8
    curve_data.use_fill_caps = True
    
    spline = curve_data.splines.new(type='CATMULL_ROM' if hasattr(curve_data.splines, 'CATMULL_ROM') else 'NURBS')
    spline.points.add(len(points) - 1)
    spline.use_endpoint_u = True
    spline.use_cyclic_u = cyclic
    
    for i, pt in enumerate(points):
        spline.points[i].co = (pt[0], pt[1], pt[2], 1.0)
        if radii and i < len(radii):
            spline.points[i].radius = radii[i]
            
    obj = bpy.data.objects.new(name, curve_data)
    return link_object(obj, collection)


def apply_boolean(target_obj, cutter_obj, operation='DIFFERENCE', hide_cutter=True):
    """
    operation: 'DIFFERENCE' | 'UNION' | 'INTERSECT'
    """
    mod = target_obj.modifiers.new(name=f"Bool_{cutter_obj.name}", type='BOOLEAN')
    mod.operation = operation
    mod.object = cutter_obj
    if hasattr(mod, "solver"):
        mod.solver = 'EXACT'
    if hide_cutter:
        cutter_obj.hide_viewport = True
        cutter_obj.hide_render = True
        cutter_obj.display_type = 'WIRE'
    return mod

def add_bevel_modifier(obj, width=0.02, segments=3, angle_limit=30.0):
    mod = obj.modifiers.new(name="Bevel", type='BEVEL')
    mod.width = width
    mod.segments = segments
    mod.limit_method = 'ANGLE'
    mod.angle_limit = math.radians(angle_limit)
    if hasattr(mod, "harden_normals"):
        mod.harden_normals = True
    return mod

def add_subsurf_modifier(obj, levels=2, render_levels=2):
    mod = obj.modifiers.new(name="Subsurf", type='SUBSURF')
    mod.levels = levels
    mod.render_levels = render_levels
    return mod

def add_mirror_modifier(obj, axis_x=True, axis_y=False, axis_z=False, mirror_object=None):
    mod = obj.modifiers.new(name="Mirror", type='MIRROR')
    mod.use_axis[0] = axis_x
    mod.use_axis[1] = axis_y
    mod.use_axis[2] = axis_z
    mod.use_clip = True
    if mirror_object:
        mod.mirror_object = mirror_object
    return mod

def add_solidify_modifier(obj, thickness=0.03, offset=-1.0):
    mod = obj.modifiers.new(name="Solidify", type='SOLIDIFY')
    mod.thickness = thickness
    mod.offset = offset
    mod.use_even_offset = True
    return mod


def create_skinned_skeleton(name, nodes_xyz, edges_idx, radii_xy, subsurf_levels=2, collection=None):
    """
    nodes_xyz: list of (x, y, z) coordinates for joints.
    edges_idx: list of (idx_a, idx_b) pairs connecting joints.
    radii_xy: list of (radius_x, radius_y) tuples corresponding to each joint.
    """
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(nodes_xyz, edges_idx, [])
    mesh.update()
    
    obj = bpy.data.objects.new(name, mesh)
    link_object(obj, collection)
    
    skin_mod = obj.modifiers.new(name="Skin", type='SKIN')
    skin_mod.use_smooth_shade = True
    
    # Set per-vertex skin radius
    skin_layer = mesh.skin_vertices[0].data
    for i, (rx, ry) in enumerate(radii_xy):
        skin_layer[i].radius = (rx, ry)
    skin_layer[0].use_root = True
    
    add_subsurf_modifier(obj, levels=subsurf_levels, render_levels=subsurf_levels)
    return obj


def set_bsdf_socket(bsdf, aliases, value):
    """Safely sets a Principled BSDF input across Blender 3.x and 4.x+."""
    for name in aliases:
        if name in bsdf.inputs:
            bsdf.inputs[name].default_value = value
            return

def create_pbr_material(
    name,
    base_color=(0.8, 0.8, 0.8, 1.0),
    metallic=0.0,
    roughness=0.4,
    specular=0.5,
    transmission=0.0,
    ior=1.45,
    emission_color=(0.0, 0.0, 0.0, 1.0),
    emission_strength=0.0,
    coat=0.0,
    bump_noise_scale=0.0,
    bump_strength=0.1
):
    """Creates a PBR material with optional procedural surface micro-bump."""
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    
    out = nodes.new(type='ShaderNodeOutputMaterial')
    out.location = (400, 0)
    
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (0, 0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    
    if len(base_color) == 3:
        base_color = (*base_color, 1.0)
    if len(emission_color) == 3:
        emission_color = (*emission_color, 1.0)
        
    set_bsdf_socket(bsdf, ['Base Color'], base_color)
    set_bsdf_socket(bsdf, ['Metallic'], metallic)
    set_bsdf_socket(bsdf, ['Roughness'], roughness)
    set_bsdf_socket(bsdf, ['Specular IOR Level', 'Specular'], specular)
    set_bsdf_socket(bsdf, ['Transmission Weight', 'Transmission'], transmission)
    set_bsdf_socket(bsdf, ['IOR'], ior)
    set_bsdf_socket(bsdf, ['Emission Color', 'Emission'], emission_color)
    set_bsdf_socket(bsdf, ['Emission Strength'], emission_strength)
    set_bsdf_socket(bsdf, ['Coat Weight', 'Clearcoat'], coat)
    
    if bump_noise_scale > 0.0:
        tex_coord = nodes.new(type='ShaderNodeTexCoord')
        tex_coord.location = (-600, -250)
        
        noise = nodes.new(type='ShaderNodeTexNoise')
        noise.location = (-400, -250)
        noise.inputs['Scale'].default_value = bump_noise_scale
        noise.inputs['Detail'].default_value = 8.0
        
        bump = nodes.new(type='ShaderNodeBump')
        bump.location = (-200, -250)
        bump.inputs['Strength'].default_value = bump_strength
        bump.inputs['Distance'].default_value = 0.02
        
        links.new(tex_coord.outputs['Object'], noise.inputs['Vector'])
        links.new(noise.outputs['Fac'], bump.inputs['Height'])
        links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])
        
    return mat

def assign_material(obj, mat):
    """Assigns a material to an object."""
    if obj.data is None or not hasattr(obj.data, "materials"):
        return
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)


def compute_scene_bounds(objects=None):
    """Returns (min_vec, max_vec, center_vec, max_dimension) of all visible meshes."""
    if objects is None:
        objects = [o for o in bpy.context.scene.objects if o.type in {'MESH', 'CURVE'} and not o.hide_render]
        
    bpy.context.view_layer.update()
    min_co = Vector((float('inf'), float('inf'), float('inf')))
    max_co = Vector((float('-inf'), float('-inf'), float('-inf')))
    
    found = False
    for obj in objects:
        for corner in obj.bound_box:
            world_corner = obj.matrix_world @ Vector(corner)
            min_co.x = min(min_co.x, world_corner.x)
            min_co.y = min(min_co.y, world_corner.y)
            min_co.z = min(min_co.z, world_corner.z)
            max_co.x = max(max_co.x, world_corner.x)
            max_co.y = max(max_co.y, world_corner.y)
            max_co.z = max(max_co.z, world_corner.z)
            found = True
            
    if not found:
        return Vector((-1, -1, -1)), Vector((1, 1, 1)), Vector((0, 0, 0)), 2.0
        
    center = (min_co + max_co) * 0.5
    dims = max_co - min_co
    max_dim = max(dims.x, dims.y, dims.z, 0.1)
    return min_co, max_co, center, max_dim

def setup_studio_camera_and_lights(azimuth_deg=-35.0, elevation_deg=25.0, distance_mult=2.4):
    """Automatically frames the model and sets up a 3-point studio lighting rig."""
    _, _, center, max_dim = compute_scene_bounds()
    dist = max_dim * distance_mult
    
    az = math.radians(azimuth_deg)
    el = math.radians(elevation_deg)
    cam_pos = center + Vector((
        dist * math.cos(el) * math.sin(az),
        -dist * math.cos(el) * math.cos(az),
        dist * math.sin(el)
    ))
    
    cam_data = bpy.data.cameras.new("StudioCamera")
    cam_data.lens = 50.0
    cam_data.clip_start = 0.01
    cam_data.clip_end = max(1000.0, dist * 10.0)
    cam_obj = bpy.data.objects.new("StudioCamera", cam_data)
    link_object(cam_obj)
    cam_obj.location = cam_pos
    
    # Aim camera directly at bounding box center
    direction = (center - cam_pos).normalized()
    cam_obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    bpy.context.scene.camera = cam_obj
    
    # 3-Point Area Lighting Rig scaled to model size
    light_specs = [
        ("KeyLight",  (dist * 1.1, -dist * 0.9, dist * 1.2), (1.0, 0.96, 0.90), 60.0 * (dist ** 2), max_dim * 0.8),
        ("FillLight", (-dist * 1.2, -dist * 0.7, dist * 0.6), (0.85, 0.92, 1.0), 25.0 * (dist ** 2), max_dim * 1.2),
        ("RimLight",  (0.0, dist * 1.1, dist * 0.9),         (1.0, 1.0, 1.0),   45.0 * (dist ** 2), max_dim * 0.7),
    ]
    for name, offset, color, energy, size in light_specs:
        l_data = bpy.data.lights.new(name=name, type='AREA')
        l_data.color = color
        l_data.energy = energy
        l_data.size = size
        l_obj = bpy.data.objects.new(name=name, object_data=l_data)
        link_object(l_obj)
        l_obj.location = center + Vector(offset)
        l_dir = (center - l_obj.location).normalized()
        l_obj.rotation_euler = l_dir.to_track_quat('-Z', 'Y').to_euler()
        
    return cam_obj

def export_and_render(blend_path=None, glb_path=None, render_path=None, res_x=1024, res_y=1024):
    """Saves .blend, exports .glb, and renders a preview image."""
    scene = bpy.context.scene
    if blend_path:
        bpy.ops.wm.save_as_mainfile(filepath=blend_path)
    if glb_path:
        bpy.ops.export_scene.gltf(filepath=glb_path, export_format='GLB', export_apply=True)
    if render_path:
        # Select EEVEE: 4.2-4.x use 'BLENDER_EEVEE_NEXT', 5.0+ uses 'BLENDER_EEVEE'
        eevee_id = 'BLENDER_EEVEE' if bpy.app.version >= (5, 0, 0) else 'BLENDER_EEVEE_NEXT'
        try:
            scene.render.engine = eevee_id
        except TypeError:
            scene.render.engine = 'CYCLES'
        scene.render.resolution_x = res_x
        scene.render.resolution_y = res_y
        scene.render.filepath = render_path
        bpy.ops.render.render(write_still=True)

// =====================================================================
//  Modular stacking enclosure
//    Layer 1 (bottom) : NOYITO 4-ch optocoupler module (24V -> 3.3V, PNP)
//    Layer 2          : Raspberry Pi 5 + official Active Cooler
//    Lid              : honeycomb vent over the Active Cooler fan
//
//  Assembly: M3 socket-head bolts run from the lid, through every layer,
//  into M3 heat-set inserts (or captive nuts) in the base layer.
//  Layers self-locate on raised collars around each bolt; the lid locates
//  with a skirt that drops inside the top layer.
//
//  Render one part at a time:
//    openscad -D 'part="base"' -o base.stl pi5_stack_enclosure.scad
//    part = "base" | "pi" | "lid" | "assembly" | "exploded"
//  All units mm. Board-relative coordinates follow the Raspberry Pi 5
//  mechanical drawing (origin = corner nearest the USB-C port).
// =====================================================================

part = "exploded";

// ---------------------------------------------------------------------
// FASTENER SCHEME -- LOCKED (agreed with Jay 2026-10-07). Do not change
// any line tagged [LOCKED] without asking first.
//   Stack: 4x M3 x 40 SHCS, lid -> Pi layer -> M3 heat-set inserts
//          (M3 x L4 x OD4.2) in the base corner collars. Heat-set because
//          the stack is opened every time the field wiring changes.
//   Pi 5:  4x M2.5 x 6 SHCS self-tapped into 2.2 mm holes (board fitted once).
//   Opto:  4x M3 x 6 SHCS self-tapped into 2.6 mm holes (board fitted once).
// ---------------------------------------------------------------------

/* ---------------- global ---------------- */
wall        = 2.4;     // side wall thickness
floor_t     = 2.0;     // floor thickness of each layer
IX          = 89.0;    // cavity length (X)  -- shared by every layer
IY          = 63.0;    // cavity width  (Y)  -- includes ~6 mm wire channel on the GPIO side
cav_r       = 1.0;     // cavity corner radius
out_r       = 3.0;     // outer corner radius

/* ---------------- stack fasteners (M3) ---------------- */
boss_off    = 2.6;     // boss centre sits this far outside each cavity corner (diagonal)
boss_r      = 4.3;
bolt_d      = 3.4;     // [LOCKED] M3 clearance
insert_d    = 4.0;     // [LOCKED] M3 heat-set insert hole (3DZWMAN kit insert M3 x L4 x OD4.2)
insert_h    = 5.0;     // [LOCKED] 4 mm insert + 1 mm for displaced plastic
base_fastener = "insert";   // [LOCKED] "insert" or "nut"
nut_af      = 5.7;     // M3 nut across-flats + clearance (nut option)
nut_h       = 2.6;
collar_d    = 6.4;     // locating collar on top of a layer that has another layer above it
collar_h    = 1.6;
collar_clr  = 0.25;    // radial clearance of the matching pocket

/* ---------------- layer heights ---------------- */
H_opto      = 28.0;    // module is 18 mm tall overall -> ~2 mm clearance under the Pi-layer floor
H_pi        = 34.0;    // room for Active Cooler + Dupont jumpers on the GPIO header
lid_t       = 5.0;
lid_skirt_h = 3.0;
lid_skirt_t = 1.4;
fit_clr     = 0.25;    // skirt-to-wall clearance
cbore_d     = 6.0;     // [LOCKED] socket-head counterbore in lid
cbore_h     = 3.3;     // [LOCKED]

/* ---------------- Raspberry Pi 5 ---------------- */
pi_L = 85;  pi_W = 56;  pcb_t = 1.6;
pi_off      = [0.6, 0.8];          // board origin inside cavity (front-left gap)
pi_holes    = [[3.5,3.5],[61.5,3.5],[3.5,52.5],[61.5,52.5]];
pi_stand_h  = 5.0;                 // clears the micro-SD card underneath
pi_stand_d  = 6.5;                 // [LOCKED]
pi_screw_d  = 2.2;                 // [LOCKED] M2.5 x 6 socket cap, self-tapped into the printed hole
pi_pcb_z    = floor_t + pi_stand_h;          // PCB underside
pi_top      = pi_pcb_z + pcb_t;              // PCB top surface

// front wall (y = 0): [x on board, width, height, centre height above PCB top]
pi_front_ports = [
    [11.2, 12.0, 7.5, 1.6],   // USB-C power
    [25.8, 11.0, 7.5, 1.5],   // micro-HDMI 0
    [39.2, 11.0, 7.5, 1.5],   // micro-HDMI 1
];
// right wall (x = end): [y on board, width, height, bottom offset from PCB top]
pi_right_ports = [
    [10.2, 16.5, 14.5, -0.6], // Gigabit Ethernet
    [29.1, 15.2, 17.0, -0.6], // USB 3.0 stack
    [47.0, 15.2, 17.0, -0.6], // USB 2.0 stack
];
// left wall (x = 0)
sd_y        = 28.0;   sd_w = 15.0;  sd_h = 4.6;          // micro-SD (below PCB)
pwr_btn_y   = 18.15;  // measured: board edge (USB-C side) -> button centre
pwr_w       = 6.5;
pwr_y0      = pwr_btn_y - pwr_w/2;  pwr_y1 = pwr_btn_y + pwr_w/2; pwr_h = 5.0;  // power-button window (above PCB)
// wire pass-through in Pi-layer floor (sits under the GPIO-side channel)
wire_slot   = [10, 76];                                   // x range in cavity
// honeycomb region over the Active Cooler, board coords [x0,y0,x1,y1]
vent_inset  = 2.9;    // honeycomb fills the whole lid inside the locating skirt
hex_r       = 3.0;   hex_wall = 1.3;

/* ---------------- NOYITO optocoupler module ---------------- */
// NOYITO board (user-measured spec): 72 x 45 x 18 mm, 40 x 37 mm hole pattern (centred), 3 mm holes.
// Screw terminals run along BOTH short (45 mm) edges: inputs on one end,
// outputs on the other. Windows are cut in the two end walls of the base.
op_L = 72;  op_W = 45;
op_hole_dx  = 40;  op_hole_dy = 37;
op_off      = [(IX-72)/2, (IY-45)/2];     // centred: ~8.5 mm wiring gap at each terminal end
op_stand_h  = 6.0;
op_stand_d  = 7.0;     // [LOCKED]
op_screw_d  = 2.6;     // [LOCKED] M3 x 6 socket cap, self-tapped into the printed hole
op_top      = floor_t + op_stand_h + pcb_t;
term_w      = 43;      // terminal-block window width (each end wall), centred on the board
term_z0     = 0.3;     // window bottom above board top
term_h      = 10.0;    // window height
foot_d      = 10.5;  foot_h = 1.0;   // rubber-foot recesses under the base

$fn = 48;

/* ===================== helpers ===================== */
module rrect(x, y, h, r) {
    translate([r, r, 0]) linear_extrude(h)
        offset(r) square([x - 2*r, y - 2*r]);
}
function boss_pts() = [
    [-boss_off, -boss_off], [IX + boss_off, -boss_off],
    [-boss_off, IY + boss_off], [IX + boss_off, IY + boss_off]];

module outer_body(h) {
    union() {
        translate([-wall, -wall, 0]) rrect(IX + 2*wall, IY + 2*wall, h, out_r);
        for (p = boss_pts()) translate([p[0], p[1], 0]) cylinder(r = boss_r, h = h);
    }
}
module cavity(z0, h) {
    difference() {
        translate([0, 0, z0]) rrect(IX, IY, h, cav_r);
        for (p = boss_pts()) translate([p[0], p[1], z0 - 1]) cylinder(r = boss_r, h = h + 2);
    }
}

// generic layer: floor + walls + bosses, collar on top optional, pocket underneath optional
module layer_shell(H, collar_top = false, pocket_bottom = false) {
    difference() {
        union() {
            outer_body(H);
            if (collar_top)
                for (p = boss_pts()) translate([p[0], p[1], H]) cylinder(d = collar_d, h = collar_h);
        }
        cavity(floor_t, H);
        for (p = boss_pts()) translate([p[0], p[1], -1]) cylinder(d = bolt_d, h = H + 10);
        if (pocket_bottom)
            for (p = boss_pts()) translate([p[0], p[1], -0.01])
                cylinder(d = collar_d + 2*collar_clr, h = collar_h + 0.25);
    }
}

// whole hexes only (no slivers), pattern centred in the given rectangle
module hex_grid(x0, y0, x1, y1, h) {
    hx = hex_r * sqrt(3) / 2;            // half width (pointy-top hex)
    dx = 2*hx + hex_wall;
    dy = dx * sqrt(3) / 2;
    nr = floor(((y1 - y0) - 2*hex_r) / dy) + 1;
    nc = floor(((x1 - x0) - 2*hx - dx/2) / dx) + 1;
    ox = x0 + ((x1 - x0) - ((nc - 1)*dx + dx/2 + 2*hx)) / 2 + hx;
    oy = y0 + ((y1 - y0) - ((nr - 1)*dy + 2*hex_r)) / 2 + hex_r;
    for (j = [0 : nr - 1], i = [0 : nc - 1])
        translate([ox + i*dx + (j % 2) * dx/2, oy + j*dy, -1])
            rotate(30) cylinder(r = hex_r, h = h + 2, $fn = 6);
}

module slot_vents(len, n, pitch, w, depth) {   // horizontal slots along X, stacked in Z
    for (k = [0 : n - 1]) translate([0, 0, k * pitch])
        hull() {
            translate([w/2, 0, 0]) rotate([-90, 0, 0]) cylinder(d = w, h = depth, $fn = 20);
            translate([len - w/2, 0, 0]) rotate([-90, 0, 0]) cylinder(d = w, h = depth, $fn = 20);
        }
}

/* ===================== LAYER 1 : optocoupler ===================== */
module opto_layer() {
    difference() {
        union() {
            layer_shell(H_opto, collar_top = true, pocket_bottom = false);
            // standoffs
            for (sx = [0, 1], sy = [0, 1])
                translate([op_off[0] + (op_L - op_hole_dx)/2 + sx*op_hole_dx,
                           op_off[1] + (op_W - op_hole_dy)/2 + sy*op_hole_dy, floor_t - 0.01])
                    cylinder(d = op_stand_d, h = op_stand_h);
        }
        // standoff screw holes
        for (sx = [0, 1], sy = [0, 1])
            translate([op_off[0] + (op_L - op_hole_dx)/2 + sx*op_hole_dx,
                       op_off[1] + (op_W - op_hole_dy)/2 + sy*op_hole_dy, 0.8])
                cylinder(d = op_screw_d, h = op_stand_h + floor_t);
        // bolt anchors
        for (p = boss_pts()) {
            if (base_fastener == "insert")
                { translate([p[0], p[1], H_opto + collar_h - insert_h]) cylinder(d = insert_d, h = insert_h + 1);
                  translate([p[0], p[1], H_opto + collar_h - insert_h - 4]) cylinder(d = bolt_d, h = 5); }
            else
                translate([p[0], p[1], -0.01]) cylinder(d = nut_af / cos(30), h = nut_h, $fn = 6);
        }
        // terminal-block windows, both end (short) walls, centred on the board
        for (xw = [1, IX + wall + 1])
            translate([xw, op_off[1] + (op_W - term_w)/2, op_top + term_z0]) rotate([0, 0, 90])
                rounded_window(term_w, term_h, wall + 2, 1.5);
        // rubber-foot recesses
        for (fx = [12, IX - 12], fy = [10, IY - 10])
            translate([fx, fy, -0.01]) cylinder(d = foot_d, h = foot_h);
        // long-wall vents
        for (yw = [-wall - 1, IY - 1])
            translate([IX/2 - 25, yw, 11]) slot_vents(50, 3, 4, 2.0, wall + 2);
    }
}

/* ===================== LAYER 2 : Raspberry Pi 5 ===================== */
module pi_layer() {
    bx = pi_off[0]; by = pi_off[1];
    difference() {
        union() {
            layer_shell(H_pi, collar_top = false, pocket_bottom = true);
            for (h = pi_holes)
                translate([bx + h[0], by + h[1], floor_t - 0.01]) cylinder(d = pi_stand_d, h = pi_stand_h);
        }
        for (h = pi_holes)
            translate([bx + h[0], by + h[1], 0.8]) cylinder(d = pi_screw_d, h = pi_stand_h + floor_t);
        // front ports (y = 0 wall)
        for (p = pi_front_ports)
            translate([bx + p[0] - p[1]/2, -wall - 1, pi_top + p[3] - p[2]/2])
                rounded_window(p[1], p[2], wall + 2, 1.5);
        // right ports (x = IX wall)
        for (p = pi_right_ports)
            translate([IX - 1, by + p[0] + p[1]/2, pi_top + p[3]]) rotate([0, 0, -90])
                rounded_window(p[1], p[2], wall + 2, 1.0);
        // left wall: micro-SD (below PCB) + power-button window (above PCB)
        translate([1, by + sd_y - sd_w/2, pi_pcb_z - sd_h + 0.6]) rotate([0, 0, 90])
            rounded_window(sd_w, sd_h, wall + 2, 1.5);
        translate([1, by + pwr_y0, pi_top - 0.2]) rotate([0, 0, 90])
            rounded_window(pwr_y1 - pwr_y0, pwr_h, wall + 2, 1.5);
        // left wall exhaust vents (Active Cooler blows sideways)
        translate([1, by + 18, pi_top + 8]) rotate([0, 0, 90])
            slot_vents(34, 4, 4, 2.0, wall + 2);
        // GPIO-side wall exhaust vents
        translate([16, IY - 1, pi_top + 12]) slot_vents(54, 3, 4, 2.0, wall + 2);
        // wire pass-through slot in floor (GPIO channel)
        translate([wire_slot[0], by + pi_W + 1.0, -1])
            rrect(wire_slot[1] - wire_slot[0], IY - (by + pi_W + 1.0) - 0.6, floor_t + 2, 1.5);
    }
}
module rounded_window(w, h, d, r) {   // window in XZ plane, extruded +Y
    hull() for (xx = [r, w - r], zz = [r, h - r])
        translate([xx, 0, zz]) rotate([-90, 0, 0]) cylinder(r = r, h = d, $fn = 20);
}

/* ===================== LID ===================== */
module lid() {
    // modelled right-side-up; printed upside down (top face on the plate)
    difference() {
        union() {
            outer_body(lid_t);
            // locating skirt that drops into the Pi layer
            translate([0, 0, -lid_skirt_h])
            difference() {
                translate([fit_clr, fit_clr, 0]) rrect(IX - 2*fit_clr, IY - 2*fit_clr, lid_skirt_h + 0.01, cav_r);
                translate([fit_clr + lid_skirt_t, fit_clr + lid_skirt_t, -1])
                    rrect(IX - 2*(fit_clr + lid_skirt_t), IY - 2*(fit_clr + lid_skirt_t), lid_skirt_h + 2, 0.5);
                for (p = boss_pts()) translate([p[0], p[1], -1]) cylinder(r = boss_r + 0.4, h = lid_skirt_h + 2);
            }
        }
        for (p = boss_pts()) {
            translate([p[0], p[1], -lid_skirt_h - 1]) cylinder(d = bolt_d, h = lid_t + lid_skirt_h + 2);
            translate([p[0], p[1], lid_t - cbore_h]) cylinder(d = cbore_d, h = cbore_h + 1);
        }
        hex_grid(vent_inset, vent_inset, IX - vent_inset, IY - vent_inset, lid_t);
    }
}

/* ===================== dummy boards (preview only) ===================== */
module pi5_dummy() {
    translate([pi_off[0], pi_off[1], pi_pcb_z]) {
        color("darkgreen") rrect(pi_L, pi_W, pcb_t, 3);
        translate([0, 0, pcb_t]) {
            color("silver") {
                translate([85 - 21 + 2.2, 10.2 - 8, 0]) cube([21, 16, 13.5]);   // Ethernet
                translate([85 - 17 + 2.2, 29.1 - 7.3, 0]) cube([17, 14.6, 16]);  // USB3
                translate([85 - 17 + 2.2, 47.0 - 7.3, 0]) cube([17, 14.6, 16]);  // USB2
                translate([11.2 - 4.5, -1.2, 0]) cube([9, 7.5, 3.2]);            // USB-C
                for (x = [25.8, 39.2]) translate([x - 3.8, -1, 0]) cube([7.6, 7.5, 3.0]); // uHDMI
            }
            color("black") translate([7.1, 50, 0]) cube([50.8, 5, 8.5]);         // GPIO header
            color("dimgray") translate([8, 8, 0]) cube([52, 40, 6]);            // heatsink
            color("gray") translate([34, 28, 6]) cylinder(d = 32, h = 6);        // fan
        }
    }
}
module opto_dummy() {
    translate([op_off[0], op_off[1], floor_t + op_stand_h]) {
        color("seagreen") cube([op_L, op_W, pcb_t]);
        color("royalblue") {
            translate([0.5, (op_W - 41)/2, pcb_t]) cube([7.5, 41, 12]);
            translate([op_L - 8, (op_W - 41)/2, pcb_t]) cube([7.5, 41, 12]);
        }
    }
}

/* ===================== output ===================== */
gap = (part == "exploded") ? 30 : 0;
if (part == "base") opto_layer();
else if (part == "pi") pi_layer();
else if (part == "lid") translate([0, 0, lid_t]) mirror([0, 0, 1]) lid();   // print orientation
else {
    color("#2b2b2b") opto_layer();
    opto_dummy();
    translate([0, 0, H_opto + gap]) { color("#3a3a3a") pi_layer(); pi5_dummy(); }
    translate([0, 0, H_opto + H_pi + 2*gap]) color("#2b2b2b", 0.9) lid();
}

echo(str("Stack bolt length (lid -> base insert): ",
         (lid_t - cbore_h) + H_pi + insert_h - 1.5, " mm  -> use M3 x 40 socket head"));
echo(str("Outer footprint: ", IX + 2*(boss_off + boss_r), " x ", IY + 2*(boss_off + boss_r),
         " mm, height ", H_opto + H_pi + lid_t, " mm"));

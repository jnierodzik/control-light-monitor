// Enclosure for the GPIO tap node: Raspberry Pi 5 + Active Cooler +
// NOYITO 4-ch optocoupler (MT-301R4P-P, 24V -> 3.3V).
//
// Two printed parts: a base (tray, walls, standoffs, port windows) and a
// screw-on lid (fan vents). Render one at a time with `part`:
//
//   openscad -D 'part="base"' -o base.stl case.scad
//   openscad -D 'part="lid"'  -o lid.stl  case.scad
//
// Layout, top view (+Y up):
//
//   +-----------------------------------------+
//   |  [out]  opto board (72 x 45)  [24V in] (O)  <- cable gland, right wall
//   |                                          |
//   |        ::::: GPIO header :::::           |
//   | vents      Pi 5 (85 x 56)          USB/ETH  <- windows, right wall
//   +-----------------------------------------+
//            USB-C  HDMI HDMI                     <- windows, front wall
//
// The opto's Pi-side terminal block (VCC GND o1-o4) sits at its left end, a
// short jumper run from the GPIO header. The 24V field wiring enters through a
// gland on the opposite wall, so machine-side wiring never crosses the Pi.
//
// Sources: Raspberry Pi 5 mechanical drawing, Active Cooler product brief,
// NOYITO listing B07TCXX2QL (45x72x18mm, holes 40x37mm). Measure your opto
// board before printing; clones of this module vary.

part = "assembly"; // [assembly, base, lid, plate]

$fn = 48;
eps = 0.01;

// ---------------------------------------------------------------- shell
wall    = 2.2;
floor_t = 2.0;
lid_t   = 2.5;
inner_x = 100;
inner_y = 114;
inner_h = 33;    // floor top to lid underside; clears straight Dupont plugs on the header
fit     = 0.3;   // clearance between mating printed parts

outer_x = inner_x + 2 * wall;
outer_y = inner_y + 2 * wall;
base_h  = floor_t + inner_h;

// Corner screw posts, centred on the outer corners so they stay clear of the
// Ethernet jack in the front-right corner.
post_r        = 4.5;
insert_d      = 4.0;  // M3 heat-set insert; use 2.6 to self-tap an M3 screw instead
insert_depth  = 6;
lid_screw_d   = 3.4;

// ---------------------------------------------------------------- Pi 5
pi_l = 85;
pi_w = 56;
pi_pcb = 1.6;
pi_standoff = 5;           // underside has components; keep >= 4
pi_hole_d = 2.3;           // self-tap M2.5; 3.5 for M2.5 heat-set inserts
pi_holes = [[3.5, 3.5], [61.5, 3.5], [3.5, 52.5], [61.5, 52.5]];
pi_eth_proud = 3;          // RJ45 overhangs the PCB edge by this much
pi_gap_right = 0.8;        // RJ45 face to inner wall
pi_gap_front = 1.5;        // PCB front edge to inner wall

pi_x   = wall + inner_x - pi_gap_right - pi_eth_proud - pi_l;
pi_y   = wall + pi_gap_front;
pi_z   = floor_t + pi_standoff;   // PCB underside
pi_top = pi_z + pi_pcb;

hdmi_windows = true;   // headless node; set false for a stiffer front wall

// ---------------------------------------------------------------- opto
opto_l = 72;
opto_w = 45;
opto_pcb = 1.6;
opto_standoff = 5;
opto_hole_d = 2.6;           // self-tap M3
opto_hole_dx = 40;           // along the 72mm length
opto_hole_dy = 37;           // along the 45mm width
opto_x = wall + 12;          // room on the left for jumpers off the output block
opto_y = pi_y + pi_w + 8;
opto_top = floor_t + opto_standoff + opto_pcb;

// ---------------------------------------------------------------- entry
gland_d = 12.7;              // PG7 gland (3-6.5mm cable); 16.2 for PG9
gland_y = opto_y + opto_w / 2;
gland_z = floor_t + 17;

// ---------------------------------------------------------------- mounting
mount_ears = true;
ear_w = 12;     // how far the ear sticks out
ear_l = 22;
ear_t = 3;
ear_slot_d = 4.5;  // #8 / M4

// ================================================================ helpers

module rrect(w, h, r) {
    offset(r) square([w - 2 * r, h - 2 * r], center = true);
}

// A rounded window through a wall whose normal is +/-Y, centred at (cx, cz).
module y_window(cx, cz, w, h, r = 1.5) {
    translate([cx, -1, cz]) rotate([-90, 0, 0])
        linear_extrude(wall + 2) rrect(w, h, r);
}

// Same, through a wall whose normal is +/-X, centred at (cy, cz).
module x_window(cy, cz, w, h, r = 1.5) {
    translate([-1, cy, cz]) rotate([0, 90, 0]) rotate([0, 0, 90])
        linear_extrude(wall + 2) rrect(w, h, r);
}

module standoff(h, d_outer, d_hole) {
    difference() {
        cylinder(h = h, d = d_outer);
        translate([0, 0, 1]) cylinder(h = h, d = d_hole);
    }
}

module corner_posts(h) {
    for (x = [0, outer_x], y = [0, outer_y])
        translate([x, y, 0]) cylinder(h = h, r = post_r);
}

module slots(n, pitch, w, l) {
    for (i = [0 : n - 1])
        translate([i * pitch, 0]) hull() {
            translate([0, -l / 2 + w / 2]) circle(d = w);
            translate([0,  l / 2 - w / 2]) circle(d = w);
        }
}

// ================================================================ base

module base() {
    difference() {
        union() {
            difference() {
                cube([outer_x, outer_y, base_h]);
                translate([wall, wall, floor_t]) cube([inner_x, inner_y, base_h]);
            }
            corner_posts(base_h);
            if (mount_ears) ears();
        }

        // lid screw inserts
        for (x = [0, outer_x], y = [0, outer_y])
            translate([x, y, base_h - insert_depth]) cylinder(h = insert_depth + 1, d = insert_d);

        // front wall: USB-C power, micro-HDMI x2. Sized for plug overmolds,
        // not the receptacles, because the plug has to reach through the wall.
        port_z = pi_top + 1.6;
        y_window(pi_x + 11.2, port_z, 12, 8);
        if (hdmi_windows) {
            y_window(pi_x + 25.8, port_z, 11, 8);
            y_window(pi_x + 39.2, port_z, 11, 8);
        }

        // right wall: Ethernet, then the two stacked USB pairs
        translate([outer_x - wall, 0, 0]) {
            x_window(pi_y + 10.2, pi_top + 7.0,  17,   15);
            x_window(pi_y + 29.1, pi_top + 8.25, 15,   17.5);
            x_window(pi_y + 47.0, pi_top + 8.25, 15,   17.5);
            // 24V field wiring
            translate([-1, gland_y, gland_z]) rotate([0, 90, 0]) cylinder(h = wall + 2, d = gland_d);
        }

        // left wall: Active Cooler exhaust. The blower pushes through the fins
        // toward this side.
        translate([-1, pi_y + 8, pi_top + 3]) rotate([90, 0, 90])
            linear_extrude(wall + 2) translate([0, 9]) slots(9, 5, 2.5, 18);

        // floor vents under the Pi, between the standoffs
        translate([pi_x + 12, pi_y + pi_w / 2, -1])
            linear_extrude(floor_t + 2) slots(10, 5, 2.5, 34);
    }

    // Pi standoffs
    for (h = pi_holes)
        translate([pi_x + h[0], pi_y + h[1], floor_t - eps])
            standoff(pi_standoff + eps, 6, pi_hole_d);

    // opto standoffs
    for (sx = [-1, 1], sy = [-1, 1])
        translate([opto_x + opto_l / 2 + sx * opto_hole_dx / 2,
                   opto_y + opto_w / 2 + sy * opto_hole_dy / 2,
                   floor_t - eps])
            standoff(opto_standoff + eps, 7, opto_hole_d);
}

module ears() {
    for (y = [outer_y * 0.25, outer_y * 0.75]) {
        for (side = [0, 1]) {
            x0 = side == 0 ? -ear_w : outer_x;
            translate([x0, y - ear_l / 2, 0]) difference() {
                hull() {
                    translate([side == 0 ? ear_w : 0, 0, 0]) cube([eps, ear_l, ear_t]);
                    translate([ear_w / 2, ear_l / 2, 0]) cylinder(h = ear_t, d = ear_l * 0.7);
                }
                // slot along Y gives some slop when lining up with existing holes
                translate([ear_w / 2, ear_l / 2, -1]) hull() {
                    translate([0, -2, 0]) cylinder(h = ear_t + 2, d = ear_slot_d);
                    translate([0,  2, 0]) cylinder(h = ear_t + 2, d = ear_slot_d);
                }
            }
        }
    }
}

// ================================================================ lid
// Modelled in place (sitting on the base, z = 0 at its underside).

lip_h = 3;
lip_t = 1.5;
lip_corner_clear = 8;   // keep the lip off the corner posts

module lid() {
    difference() {
        union() {
            cube([outer_x, outer_y, lid_t]);
            corner_posts(lid_t);
            lip();
        }

        for (x = [0, outer_x], y = [0, outer_y])
            translate([x, y, -1]) cylinder(h = lid_t + 2, d = lid_screw_d);

        // fan intake: slots over the whole Pi footprint, so the exact cooler
        // position does not matter
        translate([pi_x + 6, pi_y + pi_w / 2, -1])
            linear_extrude(lid_t + 2) slots(14, 5, 2.5, 44);
    }
}

module lip() {
    ix0 = wall + fit;
    iy0 = wall + fit;
    ix1 = wall + inner_x - fit;
    iy1 = wall + inner_y - fit;
    c = lip_corner_clear;
    translate([0, 0, -lip_h]) {
        translate([ix0, iy0 + c, 0])          cube([lip_t, iy1 - iy0 - 2 * c, lip_h]);
        translate([ix1 - lip_t, iy0 + c, 0])  cube([lip_t, iy1 - iy0 - 2 * c, lip_h]);
        translate([ix0 + c, iy0, 0])          cube([ix1 - ix0 - 2 * c, lip_t, lip_h]);
        translate([ix0 + c, iy1 - lip_t, 0])  cube([ix1 - ix0 - 2 * c, lip_t, lip_h]);
    }
}

// ================================================================ ghosts
// Rough stand-ins for the boards, for checking fit in the assembly view only.

module pi5_ghost() {
    translate([pi_x, pi_y, pi_z]) {
        color("green") cube([pi_l, pi_w, pi_pcb]);
        translate([0, 0, pi_pcb]) {
            color("silver") {
                translate([pi_l + pi_eth_proud - 21, 10.2 - 8, 0])   cube([21, 16, 13.5]);
                translate([pi_l + 2 - 17.5, 29.1 - 7.3, 0])          cube([17.5, 14.6, 16]);
                translate([pi_l + 2 - 17.5, 47.0 - 7.3, 0])          cube([17.5, 14.6, 16]);
                translate([11.2 - 4.5, -0.5, 0])                     cube([9, 7.5, 3.2]);
                for (cx = [25.8, 39.2]) translate([cx - 3.5, -0.5, 0]) cube([7, 7.5, 3]);
            }
            color("black") translate([7, 50, 0]) cube([51, 5, 8.5]);        // GPIO header
            color("lightgray") translate([3, 8.5, 0]) cube([63.5, 42.5, 13.7]); // cooler envelope
        }
    }
}

module opto_ghost() {
    translate([opto_x, opto_y, floor_t + opto_standoff]) {
        color("green") cube([opto_l, opto_w, opto_pcb]);
        translate([0, 0, opto_pcb]) color("mediumseagreen") {
            translate([-2, 2.5, 0])          cube([10, 40, 10]);   // Pi side: VCC GND o1-o4
            translate([opto_l - 8, 2.5, 0])  cube([10, 40, 10]);   // 24V side: 1- 1+ ... 4+
        }
    }
}

// ================================================================ output

if (part == "base") {
    base();
} else if (part == "lid") {
    // flipped: top face on the bed, lip pointing up
    translate([0, outer_y, lid_t]) rotate([180, 0, 0]) lid();
} else if (part == "plate") {
    base();
    translate([outer_x + 2 * post_r + 20, outer_y, lid_t]) rotate([180, 0, 0]) lid();
} else {
    base();
    %pi5_ghost();
    %opto_ghost();
    translate([0, 0, base_h + 15]) color("lightsteelblue", 0.6) lid();
}

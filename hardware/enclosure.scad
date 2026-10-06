// Enclosure for the machine stack light tap: Raspberry Pi 5 + 4-channel optocoupler
// board, side by side, with room for four Wago connectors.
//
// Print base and lid separately, both flat on the bed, no supports needed.
// PETG or ASA rather than PLA -- a closed box near machinery gets warm, and PLA
// softens around 60C.
//
// The board posts are printed plastic on purpose. They are the standoffs AND the
// isolation: the whole point of the optocouplers is that the machine's 0V and the
// Pi's 0V never meet, and metal standoffs screwed to a common plate would quietly
// undo that. Screws go straight into the plastic posts; no metal spacers.
//
//   render base:  set PART = "base"
//   render lid:   set PART = "lid"

PART = "base";          // "base" | "lid" | "both"

/* ---------------------------------------------------------------- dimensions */

wall      = 2.5;        // wall thickness
floor_t   = 2.5;        // floor thickness
inner_x   = 126;        // internal floor, long axis
inner_y   = 126;        // internal floor, short axis
inner_z   = 40;         // internal height: Pi + Active Cooler is ~20mm, rest is wiring

post_h    = 6;          // board sits this far off the floor
post_od   = 7;          // outside diameter of a board post

// Raspberry Pi 5 -- standard HAT footprint, holes 3.5mm in from each corner
pi_w      = 85;
pi_d      = 56;
pi_hole_dx = 58;        // hole spacing, long axis
pi_hole_dy = 49;        // hole spacing, short axis
pi_inset   = 3.5;       // hole centre to board edge
pi_pilot   = 2.1;       // pilot for an M2.5 screw self-tapping into plastic

// MT-301R4P-P optocoupler board -- measured
opto_w    = 72.15;
opto_d    = 45.45;
opto_hole_dx = 40;      // hole spacing, long axis
opto_hole_dy = 37.5;    // hole spacing, short axis
opto_pilot   = 2.5;     // pilot for an M3 screw
// Assumes the hole pattern is centred on the board. If the posts do not line up,
// measure one hole from a corner and replace these two with explicit offsets.
opto_inset_x = (opto_w - opto_hole_dx) / 2;
opto_inset_y = (opto_d - opto_hole_dy) / 2;

// board origins (lower-left corner of each board, in internal coordinates)
margin    = 8;
pi_x      = margin;
pi_y      = margin;
opto_x    = margin;
opto_y    = pi_y + pi_d + 8;        // 8mm gap between the two boards

// cable entries, in the wall at x = 0
hole_d    = 14;         // passes a USB-C plug or a JST-XH housing
hole_z    = floor_t + 14;
hole1_y   = 30;
hole2_y   = 90;

// lid fixing
corner_od = 9;
corner_pilot = 2.5;     // M3 self-tapping
lid_t     = 2.5;
lid_clear = 0.3;        // slip fit for the lid lip

// mounting ears
ear_len   = 14;
ear_hole  = 4.5;        // clearance for an M4 screw

$fn = 48;

/* --------------------------------------------------------------------- parts */

module post(x, y, pilot) {
    translate([x, y, floor_t]) difference() {
        cylinder(h = post_h, d = post_od);
        translate([0, 0, 1]) cylinder(h = post_h, d = pilot);
    }
}

module board_posts(ox, oy, dx, dy, inset_x, inset_y, pilot) {
    for (i = [0, 1], j = [0, 1])
        post(ox + inset_x + i * dx, oy + inset_y + j * dy, pilot);
}

module corner_posts() {
    for (x = [wall + corner_od/2, wall + inner_x - corner_od/2],
         y = [wall + corner_od/2, wall + inner_y - corner_od/2])
        translate([x, y, floor_t]) difference() {
            cylinder(h = inner_z, d = corner_od);
            translate([0, 0, 2]) cylinder(h = inner_z, d = corner_pilot);
        }
}

// Slots to zip-tie the Wago bank and the incoming cables down. Strain relief is
// what stops vibration working a connector loose over months.
module tie_slots() {
    for (p = [[104, 24], [104, 54], [104, 84], [30, 118]])
        translate([wall + p[0], wall + p[1], -1])
            hull() for (dx = [-5, 5]) translate([dx, 0, 0]) cylinder(h = floor_t + 2, d = 3.5);
}

module ears() {
    for (y = [wall + 18, wall + inner_y - 18])
        translate([-ear_len, y - 7, 0]) difference() {
            hull() {
                cube([ear_len, 14, 4]);
                translate([ear_len, 0, 0]) cube([1, 14, 10]);
            }
            translate([ear_len/2 - 1, 7, -1]) cylinder(h = 10, d = ear_hole);
        }
}

module base() {
    difference() {
        union() {
            // shell
            difference() {
                cube([inner_x + 2*wall, inner_y + 2*wall, inner_z + floor_t]);
                translate([wall, wall, floor_t])
                    cube([inner_x, inner_y, inner_z + 1]);
            }
            ears();
            translate([wall, wall, 0]) {
                board_posts(pi_x, pi_y, pi_hole_dx, pi_hole_dy, pi_inset, pi_inset, pi_pilot);
                board_posts(opto_x, opto_y, opto_hole_dx, opto_hole_dy,
                            opto_inset_x, opto_inset_y, opto_pilot);
            }
            corner_posts();
        }
        // cable entries
        for (y = [hole1_y, hole2_y])
            translate([-1, wall + y, hole_z])
                rotate([0, 90, 0]) cylinder(h = wall + 2, d = hole_d);
        tie_slots();
    }
}

module lid() {
    difference() {
        union() {
            cube([inner_x + 2*wall, inner_y + 2*wall, lid_t]);
            // lip that drops inside the walls and locates the lid
            translate([wall + lid_clear, wall + lid_clear, -3])
                cube([inner_x - 2*lid_clear, inner_y - 2*lid_clear, 3]);
        }
        for (x = [wall + corner_od/2, wall + inner_x - corner_od/2],
             y = [wall + corner_od/2, wall + inner_y - corner_od/2])
            translate([x, y, -4]) {
                cylinder(h = 10, d = 3.4);                 // M3 clearance
                translate([0, 0, 4.4]) cylinder(h = 2, d1 = 3.4, d2 = 6.4);  // countersink
            }
        // clearance so the lip does not foul the corner posts
        for (x = [wall + corner_od/2, wall + inner_x - corner_od/2],
             y = [wall + corner_od/2, wall + inner_y - corner_od/2])
            translate([x, y, -4]) cylinder(h = 4, d = corner_od + 0.6);
    }
}

if (PART == "base") base();
else if (PART == "lid") lid();
else { base(); translate([0, inner_y + 2*wall + 10, 0]) lid(); }

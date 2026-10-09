//! Shared aggregation attributes and raster-grid records.
pub(crate) const ATTRIBUTES: [&str; 8] = [
    "id",
    "id_down",
    "sub",
    "p_order",
    "unit_length",
    "upstream_length",
    "unit_area",
    "upstream_area",
];
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Attributes {
    pub integers: [i64; 4],
    pub metrics: [f64; 4],
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct Window {
    pub x: usize,
    pub y: usize,
    pub width: usize,
    pub height: usize,
}

pub(crate) struct Grid {
    pub transform: [f64; 6],
    pub width: usize,
    pub height: usize,
    pub wkt: String,
}

mod util;

use std::collections::HashMap;
use util::helper;

/// A point on the grid.
pub struct Point {
    x: i32,
    y: i32,
}

impl Point {
    /// Make a point.
    pub fn new(x: i32, y: i32) -> Self {
        Point { x, y }
    }

    fn sum(&self) -> i32 {
        helper(self.x) + self.y
    }
}

pub trait Shape {
    fn area(&self) -> f64;
}

impl Shape for Point {
    fn area(&self) -> f64 {
        0.0
    }
}

pub enum Mode {
    Fast,
    Slow,
}

const LIMIT: i32 = 10;

fn main() {
    let p = Point::new(1, 2);
    let mut m: HashMap<i32, i32> = HashMap::new();
    m.insert(p.sum(), LIMIT);
    println!("{}", p.area());
}

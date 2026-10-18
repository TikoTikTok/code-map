#include "shape.hpp"
#include <iostream>

double geo::Circle::area() const {
    return 3.14159 * r_ * r_;
}

int main() {
    geo::Circle c(1.0);
    std::cout << c.name() << " " << geo::twice(c.area()) << "\n";
    return 0;
}

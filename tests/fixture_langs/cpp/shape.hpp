#pragma once
#include <string>

namespace geo {

/// Base shape.
class Shape {
public:
    virtual ~Shape() = default;
    virtual double area() const = 0;
    std::string name() const { return "shape"; }
};

class Circle : public Shape {
public:
    explicit Circle(double r) : r_(r) {}
    double area() const override;
private:
    double r_;
};

template <typename T>
T twice(T v) { return v * 2; }

}  // namespace geo

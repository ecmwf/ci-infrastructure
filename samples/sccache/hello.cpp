// SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
//
// SPDX-License-Identifier: Apache-2.0

// No __DATE__/__TIME__: sccache keys on preprocessed text.

#include <iostream>
#include <numeric>
#include <string>
#include <vector>

namespace {

template <typename T>
T sum_of_squares(const std::vector<T>& values) {
    return std::accumulate(values.begin(), values.end(), T{}, [](T acc, T v) { return acc + v * v; });
}

}  // namespace

int main() {
    std::vector<int> values(16);
    std::iota(values.begin(), values.end(), 1);

    const int total = sum_of_squares(values);
    const std::string label = "sum_of_squares(1..16)";

    std::cout << label << " = " << total << "\n";
    return total > 0 ? 0 : 1;
}

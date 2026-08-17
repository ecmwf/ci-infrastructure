// SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
//
// SPDX-License-Identifier: Apache-2.0

// One translation unit with enough template instantiation to be a genuine
// compile rather than a no-op, but small enough that a cache miss costs a
// second. Deliberately free of __DATE__/__TIME__ and of anything else that
// would differ between two compiles of the same source: sccache keys on the
// preprocessed text, so a varying macro would turn every build into a miss and
// the probe could never observe a cache hit.

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

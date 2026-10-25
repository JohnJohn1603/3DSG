#include <CGAL/Exact_predicates_inexact_constructions_kernel.h>
#include <CGAL/Constrained_Delaunay_triangulation_2.h>
#include <CGAL/Constrained_triangulation_face_base_2.h>
#include <CGAL/Triangulation_data_structure_2.h>
#include <CGAL/Triangulation_vertex_base_2.h>
#include <gmp.h>
#include <mpfr.h>

#include <iostream>

int main() {
  using Kernel = CGAL::Exact_predicates_inexact_constructions_kernel;
  using VertexBase = CGAL::Triangulation_vertex_base_2<Kernel>;
  using FaceBase = CGAL::Constrained_triangulation_face_base_2<Kernel>;
  using TDS = CGAL::Triangulation_data_structure_2<VertexBase, FaceBase>;
  using CDT = CGAL::Constrained_Delaunay_triangulation_2<
      Kernel, TDS, CGAL::Exact_predicates_tag>;

  CDT triangulation;
  const auto a = triangulation.insert(Kernel::Point_2(0.0, 0.0));
  const auto b = triangulation.insert(Kernel::Point_2(4.0, 0.0));
  const auto c = triangulation.insert(Kernel::Point_2(4.0, 3.0));
  const auto d = triangulation.insert(Kernel::Point_2(0.0, 3.0));
  triangulation.insert_constraint(a, b);
  triangulation.insert_constraint(b, c);
  triangulation.insert_constraint(c, d);
  triangulation.insert_constraint(d, a);
  triangulation.insert_constraint(a, c);

  mpz_t integer_probe;
  mpz_t expected_integer;
  mpz_init(integer_probe);
  mpz_init(expected_integer);
  mpz_set_str(integer_probe, "12345678901234567890", 10);
  mpz_mul_ui(integer_probe, integer_probe, 3UL);
  mpz_set_str(expected_integer, "37037036703703703670", 10);
  const bool gmp_ok = mpz_cmp(integer_probe, expected_integer) == 0;

  mpfr_t float_probe;
  mpfr_t lower_bound;
  mpfr_t upper_bound;
  mpfr_init2(float_probe, 128);
  mpfr_init2(lower_bound, 128);
  mpfr_init2(upper_bound, 128);
  mpfr_set_ui(float_probe, 2UL, MPFR_RNDN);
  mpfr_sqrt(float_probe, float_probe, MPFR_RNDN);
  mpfr_set_str(lower_bound, "1.414213562373095", 10, MPFR_RNDN);
  mpfr_set_str(upper_bound, "1.414213562373096", 10, MPFR_RNDN);
  const bool mpfr_ok = mpfr_cmp(float_probe, lower_bound) > 0
      && mpfr_cmp(float_probe, upper_bound) < 0;

  std::size_t constrained_edges = 0;
  for (const auto edge : triangulation.finite_edges()) {
    constrained_edges += triangulation.is_constrained(edge) ? 1U : 0U;
  }
  const bool cgal_ok = triangulation.number_of_vertices() == 4
      && constrained_edges == 5
      && CGAL::orientation(Kernel::Point_2(0, 0), Kernel::Point_2(1, 0),
                           Kernel::Point_2(0, 1)) == CGAL::LEFT_TURN;
  std::cout << "CGAL CDT vertices=" << triangulation.number_of_vertices()
            << " constrained_edges=" << constrained_edges
            << "; GMP multiply=" << (gmp_ok ? "ok" : "FAIL")
            << "; MPFR sqrt2=" << (mpfr_ok ? "ok" : "FAIL") << '\n';
  mpfr_clear(float_probe);
  mpfr_clear(lower_bound);
  mpfr_clear(upper_bound);
  mpz_clear(expected_integer);
  mpz_clear(integer_probe);
  return cgal_ok && gmp_ok && mpfr_ok ? 0 : 1;
}

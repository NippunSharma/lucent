from manim_imports_ext import *


class Banner(InteractiveScene):
    def construct(self):
        # Test
        frame = self.frame
        self.set_floor_plane("xz")

        n_walks = 30
        line_groups = VGroup()
        all_dots = Group()
        for n in range(n_walks):
            line, dots = self.get_random_walk()
            line.set_stroke(opacity=random.random()**2)
            line_groups.add(line)
            all_dots.add(dots)

        line_groups.set_stroke(flat=True)

        # Animate
        frame.set_field_of_view(0.1 * DEG)
        frame.reorient(45, -35, 0, (4.49, 2.29, -0.07), 5.96)
        self.play(
            LaggedStart(
                (ShowCreation(line, rate_func=linear)
                for line in line_groups),
                lag_ratio=0.1,
            ),
            LaggedStartMap(FadeIn, all_dots, lag_ratio=0.1, time_span=(0, 5)),
            frame.animate.reorient(0, 0, 0, (4.5, 2, 0), 5.96).set_anim_args(time_span=(7, 15)),
            run_time=15,
        )

    def get_random_walk(self, n_steps=25):
        line = VMobject()
        point = ORIGIN.copy()
        line.start_new_path(ORIGIN)

        choices = [UP, UP, UP, DOWN, LEFT, RIGHT, RIGHT, RIGHT, RIGHT, RIGHT, IN, IN, OUT, OUT]

        all_points = [point.copy()]
        for n in range(n_steps):
            point += random.choice(choices)
            line.add_line_to(point)
            all_points.append(point.copy())

        line.set_stroke([BLUE_E, TEAL], width=3, opacity=0.5)
        dots = DotCloud(all_points)
        dots.set_color(WHITE, 0.1)

        return Group(line, dots)


class WriteNameToUrl(InteractiveScene):
    def construct(self):
        # Test
        full_name = Text("3b1b Talent", font_size=90)
        url = Text("3b1b.co/talent", font_size=90)
        url.shift((full_name["3"].get_y(UP) - url["3"].get_y(UP)) * UP)
        VGroup(full_name, url).set_backstroke(BLACK, 10)

        back_rect = BackgroundRectangle(url)
        back_rect.set_fill(BLACK, 0.9)
        back_rect.set_z_index(-1)

        self.play(Write(full_name, stroke_color=WHITE, run_time=2))
        self.wait()
        self.play(LaggedStart(
            FadeIn(back_rect),
            ReplacementTransform(full_name["3b1b"], url["3b1b"]),
            Write(url[".co/"].set_stroke(behind=True), stroke_color=WHITE),
            ReplacementTransform(full_name["Talent"], url["talent"]),
            lag_ratio=0.25,
        ))
        self.wait()

from manim_imports_ext import *


class Ladybug(InteractiveScene):
    random_seed = 0

    def construct(self):
        # Add clock
        clock = self.get_clock()
        clock_points = [tick.get_start() for tick in clock.ticks]
        clock_anim = cycle_animation(ClockPassesTime(clock, 12 * 60, 12))
        self.add(clock_anim)

        # Lady bug lands on it
        ladybug = SVGMobject("ladybug")
        ladybug.set_height(0.7)
        ladybug.set_color(GREY_A)
        ladybug.set_shading(0.5, 0.5, 0)
        circle = Dot(fill_color=RED_E, radius=0.36 * ladybug.get_height())
        circle.move_to(ladybug, DOWN)
        bug = Group(circle, Point(), ladybug)
        bug.move_to(clock.ticks[0].get_start())

        path = VMobject()
        path.start_new_path(ORIGIN)
        for n in range(5):
            step = rotate_vector(RIGHT, PI * random.random())
            path.add_line_to(path.get_end() + step)
        path.make_smooth()
        path.put_start_and_end_on(7 * LEFT, clock_points[0])

        self.play(MoveAlongPath(bug, path, run_time=3))
        self.play(clock.numbers[0].animate.set_color(RED))

        bug.shift(UP)

        # Run simulation
        curr_number = 0
        covered_numbers = {0}
        while len(covered_numbers) < 12:
            step = random.choice([+1, -1])
            next_number = curr_number + step
            path_arc = -step * TAU / 12
            arrow = Arrow(
                1.2 * clock_points[curr_number],
                1.2 * clock_points[next_number],
                buff=0,
                fill_color=YELLOW,
                path_arc=path_arc,
                thickness=5,
            )

            end_color = RED
            if len(covered_numbers) == 11 and next_number not in covered_numbers:
                end_color = TEAL
            self.play(
                VFadeInThenOut(arrow),
                bug.animate.move_to(clock_points[next_number]).set_anim_args(path_arc=path_arc, time_span=(0, 0.5)),
                clock.numbers[next_number].animate.set_color(end_color)
            )
            curr_number = next_number
            covered_numbers.add(curr_number)

    def get_clock(self, radius=2):
        # Add clock (Todo, add these modifications as options to the Clock class)
        clock = Clock()
        clock.set_height(2 * radius)
        for line in [clock.hour_hand, clock.minute_hand, *clock.ticks]:
            line.scale(0.75, about_point=line.get_start())

        numbers = VGroup(Integer(n) for n in [12, *range(1, 12)])
        for number, theta in zip(numbers, np.arange(0, TAU, TAU / 12)):
            number.move_to(0.75 * radius * rotate_vector(UP, -theta))

        clock.numbers = numbers
        clock.add(numbers)
        return clock


class Question(InteractiveScene):
    def construct(self):
        text = Text("""
            What is the probability that
            the last number painted is 6?
        """)
        text.to_edge(UP)
        self.play(Write(text))
        self.wait()

"""구구단 출력 프로그램."""


def print_table(dan: int) -> None:
    """지정한 단을 출력한다."""
    for number in range(1, 10):
        print(f"{dan} x {number} = {dan * number}")


def print_all_tables() -> None:
    """2단부터 9단까지 모두 출력한다."""
    for dan in range(2, 10):
        print_table(dan)
        print()


def main() -> None:
    """구구단 출력 프로그램을 실행한다."""
    user_input = input(
        "출력할 단을 입력하세요 (2~9, 전체는 Enter/0/all): "
    ).strip().lower()

    if user_input in ("", "0", "전체", "all"):
        print_all_tables()
        return

    try:
        dan = int(user_input)
    except ValueError:
        print("2~9 사이의 숫자 또는 '전체'를 입력하세요.")
        return

    if 2 <= dan <= 9:
        print_table(dan)
    else:
        print("구구단은 2단부터 9단까지만 입력할 수 있습니다.")


if __name__ == "__main__":
    main()

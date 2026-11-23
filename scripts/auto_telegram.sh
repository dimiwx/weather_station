while true; do
  echo "Starting Telegram notifications"
  python3 /home/dimitris/weather_station/notif_telegram.py
  echo "Το script σταμάτησε με κωδικό εξόδου $?. Επανεκκίνηση σε 10 δευτερόλεπτα..."
  sleep 10
done

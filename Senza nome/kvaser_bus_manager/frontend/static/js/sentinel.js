// The Sentinel Interactive Logic

document.addEventListener('DOMContentLoaded', () => {
    console.log('The Sentinel Protocol Initiated');

    const scanBtn = document.querySelector('.sentinel-btn');
    const engageBtn = document.querySelector('.sentinel-btn.warning');
    const statusValue = document.querySelector('.status-row .value.blink');

    // Button Interaction
    scanBtn.addEventListener('click', () => {
        // Simulate scanning process
        statusValue.textContent = 'SCANNING...';
        statusValue.style.color = '#fff';

        setTimeout(() => {
            statusValue.textContent = 'TARGET LOCKED';
            statusValue.style.color = 'var(--neon-red)';
            statusValue.classList.add('blink');
        }, 2000);
    });

    engageBtn.addEventListener('click', () => {
        alert('ACCESS DENIED: AUTHORIZATION LEVEL 5 REQUIRED');
    });

    // Parallax Effect for Background
    document.addEventListener('mousemove', (e) => {
        const bg = document.querySelector('.sentinel-bg');
        const x = (window.innerWidth - e.pageX * 2) / 100;
        const y = (window.innerHeight - e.pageY * 2) / 100;

        bg.style.transform = `scale(1.05) translate(${x}px, ${y}px)`;
    });
});
